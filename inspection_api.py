from contextlib import closing
from datetime import datetime, timezone
from os import getenv
from pathlib import Path as FilePath
import base64
import binascii
import hashlib
import json
import re
from time import time
from xml.etree.ElementTree import ParseError
from regional_recalls import regional_notices
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from typing import Annotated, Literal
from uuid import UUID

import pyodbc
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import AwareDatetime, BaseModel, Field, HttpUrl, StringConstraints, model_validator

from staff_access import StaffAccess, audited, current_staff, load_staff, require_workshop_event_role

app = FastAPI(title="Vehicle Inspection API")
staff_access = StaffAccess(
    load_staff(FilePath(__file__).with_name("workshop_staff.json"))
    if getenv("INSPECTION_REQUIRE_STAFF_LOGIN") == "1" else {}
)
app.middleware("http")(staff_access.dispatch)


@app.get("/", include_in_schema=False)
@app.get("/advisor", include_in_schema=False)
@app.get("/technician", include_in_schema=False)
@app.get("/supervisor", include_in_schema=False)
@app.get("/quality-control", include_in_schema=False)
def open_inspection_app(request: Request):
    actor = current_staff.get()
    if actor and request.url.path != "/" + actor.role:
        return RedirectResponse("/" + actor.role)
    return FileResponse(FilePath(__file__).with_name("inspection_ui.html"), media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/healthz", include_in_schema=False)
def health_check():
    return {"application": "vehicle-inspection", "supervisor_overview": "reporting-handoffs-v4"}


@app.get("/staff/session")
def staff_session():
    actor = current_staff.get()
    return {"role": actor.role if actor else None, "name": actor.name if actor else None}


def connect_to_database(*, autocommit: bool = False):
    connection_string = getenv("INSPECTION_DB_CONNECTION")
    if not connection_string:
        raise RuntimeError("Set INSPECTION_DB_CONNECTION to a SQL Server connection string.")
    return pyodbc.connect(connection_string, timeout=5, autocommit=autocommit)


SetupCode = Annotated[str, StringConstraints(
    strip_whitespace=True, to_upper=True, max_length=30, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
)]


Powertrain = Literal["EV", "ICE", "HYBRID"]


VIN = Annotated[str, StringConstraints(
    strip_whitespace=True, to_upper=True, pattern=r"^[A-HJ-NPR-Z0-9a-hj-npr-z]{17}$"
)]
VehicleSpec = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class VehicleSpecifications(BaseModel):
    vin: VIN | None = None
    engine: VehicleSpec | None = None
    transmission: VehicleSpec | None = None


class RegisterVehicle(VehicleSpecifications):
    fleet_code: SetupCode
    model_year: int | None = Field(default=None, ge=1900, le=2100)
    make: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    powertrain: Powertrain | None = None


class RegisteredVehicle(RegisterVehicle):
    vehicle_id: int


class UpdateVehicle(BaseModel):
    vin: VIN | None = None
    fleet_code: SetupCode
    model_year: int = Field(ge=1900, le=2100)
    make: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    powertrain: Powertrain


def is_duplicate_key(error: pyodbc.IntegrityError) -> bool:
    return bool(re.search(r"\((2601|2627)\)", str(error)))


@app.post("/vehicles", status_code=201)
def register_vehicle(vehicle: RegisterVehicle) -> RegisteredVehicle:
    try:
        with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
            vehicle_id = cursor.execute(
                """
                INSERT dbo.Vehicle (FleetCode, ModelYear, Make, Model, Powertrain,
                                    VIN, EngineDescription, TransmissionDescription)
                OUTPUT INSERTED.VehicleId
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                vehicle.fleet_code, vehicle.model_year, vehicle.make, vehicle.model, vehicle.powertrain,
                vehicle.vin, vehicle.engine, vehicle.transmission,
            ).fetchval()
            db.commit()
    except pyodbc.IntegrityError as error:
        if is_duplicate_key(error):
            message = "This VIN is already registered" if "UX_Vehicle_VIN" in str(error) else "This fleet code is already registered"
            raise HTTPException(status_code=409, detail=message) from error
        raise
    return RegisteredVehicle(vehicle_id=vehicle_id, **vehicle.model_dump())


@app.get("/vehicle-catalog/international", include_in_schema=False)
def open_international_vehicle_catalog():
    return FileResponse(FilePath(__file__).with_name("international_vehicle_catalog.json"), media_type="application/json")


@app.get("/vehicle-catalog/sources", include_in_schema=False)
def open_vehicle_catalog_sources():
    return FileResponse(FilePath(__file__).with_name("vehicle_catalog_sources.html"), media_type="text/html")


class VehicleCatalogEntry(BaseModel):
    make: str
    model: str
    model_year: int | None


@app.get("/vehicle-catalog")
def list_vehicle_catalog() -> list[VehicleCatalogEntry]:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        rows = cursor.execute(
            """
            SELECT DISTINCT Make, Model, ModelYear
            FROM dbo.Vehicle
            ORDER BY Make, Model, ModelYear DESC;
            """
        ).fetchall()
    return [VehicleCatalogEntry(make=row.Make, model=row.Model, model_year=row.ModelYear) for row in rows]


@app.get("/vehicles/{fleet_code}")
def get_vehicle(fleet_code: str) -> RegisteredVehicle:
    return lookup_vehicle(fleet_code=fleet_code)


class DecodedVehicle(BaseModel):
    make: str | None
    model: str | None
    model_year: int | None
    powertrain: Powertrain | None


def decoded_vehicle(row: dict) -> DecodedVehicle:
    def value(key: str) -> str:
        return str(row.get(key) or "").strip()

    electrification = value("ElectrificationLevel").casefold()
    fuels = {value("FuelTypePrimary").casefold(), value("FuelTypeSecondary").casefold()} - {""}
    combustion = fuels & {"gasoline", "diesel", "ethanol (e85)", "compressed natural gas (cng)",
                         "liquefied petroleum gas (propane or lpg)", "methanol (m85)", "flexible fuel vehicle (ffv)"}
    powertrain = None
    if "hybrid" in electrification or ("electric" in fuels and combustion):
        powertrain = "HYBRID"
    elif "battery electric" in electrification or fuels == {"electric"}:
        powertrain = "EV"
    elif combustion and not electrification:
        powertrain = "ICE"

    year = value("ModelYear")
    return DecodedVehicle(
        make=value("Make") or None,
        model=value("Model") or None,
        model_year=int(year) if re.fullmatch(r"(19|20)[0-9]{2}|2100", year) else None,
        powertrain=powertrain,
    )


@app.get("/vin-decode/{vin}")
def decode_vin(vin: VIN) -> DecodedVehicle:
    try:
        with urlopen(f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json", timeout=5) as response:
            row = json.load(response)["Results"][0]
        decoded = decoded_vehicle(row)
    except (URLError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
        raise HTTPException(status_code=502, detail="VIN decoder unavailable. Enter vehicle details manually.") from error
    if not any((decoded.make, decoded.model, decoded.model_year)):
        raise HTTPException(status_code=404, detail="VIN details unavailable. Enter vehicle details manually.")
    return decoded


@app.get("/recall-campaigns")
def recall_campaigns(make: VehicleSpec, model: VehicleSpec, model_year: Annotated[int, Query(ge=1886, le=2100)]):
    query = urlencode({"make": make, "model": model, "modelYear": model_year})
    try:
        try:
            response = urlopen("https://api.nhtsa.gov/recalls/recallsByVehicle?" + query, timeout=10)
        except HTTPError as error:
            if error.code != 400:
                error.close()
                raise
            response = error
        with response:
            data = json.load(response)
            rows = data["results"]
            if not isinstance(rows, list) or data["Count"] != len(rows) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("Invalid campaign response")
            if response.status == 400 and (rows or data.get("Message") != "Results returned successfully"):
                raise ValueError("Campaign query rejected")
            return {"Count": len(rows), "results": rows}
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise HTTPException(status_code=502, detail="U.S. campaign service unavailable.") from error


@app.get("/regional-recall-notices")
def regional_recall_notices(region: Literal["ca", "gb", "eu", "au", "nz", "sa", "ae", "jp", "kr", "cn"],
                            make: VehicleSpec, model: VehicleSpec):
    try:
        return regional_notices(region, make, model, int(time() // 3600))
    except (OSError, ValueError, KeyError, TypeError, ParseError) as error:
        raise HTTPException(status_code=502, detail="Regional recall search unavailable.") from error


@app.get("/vehicle-vins/{vin}")
def get_vehicle_by_vin(vin: VIN) -> RegisteredVehicle:
    return lookup_vehicle(vin=vin)


def lookup_vehicle(*, fleet_code: str | None = None, vin: str | None = None) -> RegisteredVehicle:
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            row = cursor.execute(
                """
                SELECT VehicleId, FleetCode, ModelYear, Make, Model, Powertrain,
                       VIN, EngineDescription, TransmissionDescription
                FROM dbo.Vehicle
                WHERE FleetCode = ? OR VIN = ?;
                """,
                fleet_code, vin,
            ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    return vehicle_from_row(row)


def vehicle_from_row(row) -> RegisteredVehicle:
    return RegisteredVehicle(
        vehicle_id=row.VehicleId, fleet_code=row.FleetCode, model_year=row.ModelYear,
        make=row.Make, model=row.Model, powertrain=row.Powertrain, vin=row.VIN,
        engine=row.EngineDescription, transmission=row.TransmissionDescription,
    )


@app.patch("/vehicles/by-id/{vehicle_id}")
def update_vehicle(
    vehicle_id: Annotated[int, Path(gt=0, le=2147483647)], update: UpdateVehicle,
) -> RegisteredVehicle:
    try:
        with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
            row = cursor.execute(
                """
                UPDATE dbo.Vehicle
                SET FleetCode = ?, Make = ?, Model = ?, ModelYear = ?, Powertrain = ?,
                    VIN = COALESCE(?, VIN)
                OUTPUT inserted.VehicleId, inserted.FleetCode, inserted.ModelYear,
                       inserted.Make, inserted.Model, inserted.Powertrain, inserted.VIN,
                       inserted.EngineDescription, inserted.TransmissionDescription
                WHERE VehicleId = ?;
                """,
                update.fleet_code, update.make, update.model, update.model_year, update.powertrain,
                update.vin, vehicle_id,
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            db.commit()
    except pyodbc.IntegrityError as error:
        if is_duplicate_key(error):
            message = "This VIN is already registered" if "UX_Vehicle_VIN" in str(error) else "This fleet code is already registered"
            raise HTTPException(status_code=409, detail=message) from error
        raise
    return vehicle_from_row(row)


class SetVehiclePowertrain(VehicleSpecifications):
    powertrain: Powertrain


@app.patch("/vehicles/{fleet_code}/powertrain")
def set_vehicle_powertrain(fleet_code: SetupCode, update: SetVehiclePowertrain):
    try:
        with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
            vehicle_id = cursor.execute(
                """
                UPDATE dbo.Vehicle
                SET Powertrain = ?, VIN = COALESCE(?, VIN),
                    EngineDescription = COALESCE(?, EngineDescription),
                    TransmissionDescription = COALESCE(?, TransmissionDescription)
                OUTPUT inserted.VehicleId
                WHERE FleetCode = ?;
                """,
                update.powertrain, update.vin, update.engine, update.transmission, fleet_code,
            ).fetchval()
            if vehicle_id is None:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            db.commit()
    except pyodbc.IntegrityError as error:
        if is_duplicate_key(error):
            raise HTTPException(status_code=409, detail="This VIN is already registered") from error
        raise
    return {"fleet_code": fleet_code, "powertrain": update.powertrain}


class NewChecklistItem(BaseModel):
    item_code: SetupCode
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    fails_inspection: bool


class CreateChecklistVersion(BaseModel):
    version_code: SetupCode
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    items: list[NewChecklistItem] = Field(min_length=1, max_length=100)


class CreatedChecklistVersion(CreateChecklistVersion):
    checklist_version_id: int


@app.post("/checklists", status_code=201)
def create_checklist_version(checklist: CreateChecklistVersion) -> CreatedChecklistVersion:
    if len({item.item_code for item in checklist.items}) != len(checklist.items):
        raise HTTPException(status_code=422, detail="Each checklist item needs a unique code")

    try:
        with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
            version_id = cursor.execute(
                """
                INSERT dbo.ChecklistVersion (VersionCode, Title)
                OUTPUT INSERTED.ChecklistVersionId
                VALUES (?, ?);
                """,
                checklist.version_code, checklist.title,
            ).fetchval()
            cursor.executemany(
                """
                INSERT dbo.ChecklistItem (ChecklistVersionId, ItemCode, ItemLabel, FailInspectionOnFailure)
                VALUES (?, ?, ?, ?);
                """,
                [(version_id, item.item_code, item.label, item.fails_inspection) for item in checklist.items],
            )
            db.commit()
    except pyodbc.IntegrityError as error:
        if is_duplicate_key(error):
            raise HTTPException(status_code=409, detail="This checklist version code already exists") from error
        raise
    return CreatedChecklistVersion(checklist_version_id=version_id, **checklist.model_dump())


@app.get("/checklists/{version_code}")
def get_checklist(version_code: str):
    return load_checklist(version_code=version_code)


@app.get("/checklist-versions/{version_id}")
def get_checklist_by_id(version_id: Annotated[int, Path(gt=0, le=2147483647)]):
    return load_checklist(version_id=version_id)


# Specific systems take precedence over general words such as "warning" or "electrical".
INSPECTION_SYSTEMS = (
    ("Driver assistance", r"adas|driver assistance|adaptive cruise|lane assist|blind spot|parking sensor"),
    ("Visibility and glass", r"visibility|windshield|windscreen|wiper|washer|mirrors?"),
    ("Body and structure", r"body structure|body latches|chassis|underbody|latches"),
    ("Seats and restraints", r"restraints?|airbag|seat belts?|srs"),
    ("Climate and ventilation", r"climate|ventilation|air conditioning|hvac"),
    ("Instruments and displays", r"instruments?|driver display|instrument cluster|speed indication"),
    ("Fuel and exhaust", r"fuel exhaust|fuel leaks?|emissions|exhaust condition"),
    ("Electric thermal management", r"ev thermal|electric powertrain thermal"),
    ("EV charging and battery", r"hv indicators|high voltage|traction battery|charge port|charging"),
    ("Lights and electrical", r"lights external|low voltage"),
    ("Lights and electrical", r"brake light|headlight|tail light|exterior light|signal light|electrical"),
    ("Brakes", r"brake|parking brake"),
    ("Steering and suspension", r"steering|suspension|alignment"),
    ("Tires and wheels", r"tire|tyre|wheel"),
    ("Transmission and drivetrain", r"transmission|gearbox|clutch|drivetrain"),
    ("EV charging and battery", r"charge|charging|high voltage battery|traction battery"),
    ("Hybrid system", r"hybrid"),
    ("Engine and cooling", r"engine|coolant|cooling|exhaust"),
)


def checklist_system(item_code: str, label: str) -> str:
    text = f"{item_code} {label}".replace("_", " ").lower()
    for name, pattern in INSPECTION_SYSTEMS:
        if re.search(rf"\b(?:{pattern})\b", text):
            return name
    return "Other checks"


def load_checklist(*, version_code: str | None = None, version_id: int | None = None):
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            rows = cursor.execute(
                """
                SELECT cv.ChecklistVersionId, cv.VersionCode, cv.Title,
                       ci.ItemCode, ci.ItemLabel, ci.FailInspectionOnFailure
                FROM dbo.ChecklistVersion AS cv
                LEFT JOIN dbo.ChecklistItem AS ci
                    ON ci.ChecklistVersionId = cv.ChecklistVersionId
                WHERE cv.VersionCode = ? OR cv.ChecklistVersionId = ?
                ORDER BY ci.ItemCode
                """,
                version_code,
                version_id,
            ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="Checklist version not found")

    version = rows[0]
    return {
        "checklist_version_id": version.ChecklistVersionId,
        "version_code": version.VersionCode,
        "title": version.Title,
        "items": [
            {
                "item_code": row.ItemCode,
                "label": row.ItemLabel,
                "system": checklist_system(row.ItemCode, row.ItemLabel),
                "fails_inspection": bool(row.FailInspectionOnFailure),
            }
            for row in rows
            if row.ItemCode is not None
        ],
    }


class CreateInspectionDraft(BaseModel):
    vehicle_id: int = Field(gt=0, le=2147483647)
    checklist_version_id: int | None = Field(default=None, gt=0, le=2147483647)


@app.post("/inspection-drafts", status_code=201)
def create_inspection_draft(draft: CreateInspectionDraft):
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            row = cursor.execute(
                """
                SET NOCOUNT ON;
                INSERT dbo.InspectionDraft (VehicleId, ChecklistVersionId)
                OUTPUT inserted.DraftId, inserted.Revision, inserted.ChecklistVersionId
                SELECT v.VehicleId, cv.ChecklistVersionId
                FROM dbo.Vehicle AS v
                LEFT JOIN dbo.ActivePowertrainChecklist AS active ON active.Powertrain = v.Powertrain
                JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = COALESCE(?, active.ChecklistVersionId)
                WHERE v.VehicleId = ?;
                """,
                draft.checklist_version_id,
                draft.vehicle_id,
            ).fetchone()

            if row is None:
                if draft.checklist_version_id is not None:
                    raise HTTPException(status_code=404, detail="Vehicle or checklist version not found")
                vehicle = cursor.execute(
                    "SELECT Powertrain FROM dbo.Vehicle WHERE VehicleId = ?", draft.vehicle_id
                ).fetchone()
                if vehicle is None:
                    raise HTTPException(status_code=404, detail="Vehicle not found")
                if vehicle.Powertrain is None:
                    raise HTTPException(status_code=409, detail="Set this vehicle's powertrain in Vehicle registration and details.")
                raise HTTPException(status_code=409, detail=f"No active checklist is configured for {vehicle.Powertrain}.")
        db.commit()

    return {
        "draft_id": str(row.DraftId),
        "revision": bytes(row.Revision).hex(),
        "vehicle_id": draft.vehicle_id,
        "checklist_version_id": row.ChecklistVersionId,
    }


class ChecklistAnswer(BaseModel):
    itemCode: str = Field(min_length=1, max_length=30)
    outcome: Literal["PASS", "FAIL"]


class DraftDefect(BaseModel):
    itemCode: str = Field(min_length=1, max_length=30)
    severity: Literal["MINOR", "MAJOR", "CRITICAL"]
    finding: str = Field(min_length=1, max_length=500)


class DraftContents(BaseModel):
    answers: list[ChecklistAnswer] = Field(default_factory=list)
    defects: list[DraftDefect] = Field(default_factory=list)
    repairNote: str | None = Field(default=None, max_length=500)
    repairContext: str | None = None


class UpdateInspectionDraft(DraftContents):
    revision: str = Field(pattern=r"^[0-9a-fA-F]{16}$")
    answers: list[ChecklistAnswer]
    defects: list[DraftDefect]


class ResumableDraft(BaseModel):
    draft_id: UUID
    fleet_code: str
    checklist_version_id: int
    version_code: str
    repair_case_id: int | None
    is_initial_case_draft: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row, **details):
        return cls(
            draft_id=row.DraftId,
            fleet_code=row.FleetCode,
            checklist_version_id=row.ChecklistVersionId,
            version_code=row.VersionCode,
            repair_case_id=row.RepairCaseId,
            is_initial_case_draft=bool(row.IsInitialCaseDraft),
            created_at=row.CreatedAt.replace(tzinfo=timezone.utc),
            **details,
        )


class ResumedDraft(ResumableDraft):
    revision: str
    contents: DraftContents


@app.get("/inspection-drafts")
def list_inspection_drafts(
    fleet_code: Annotated[str, Query(min_length=1, max_length=30)],
) -> list[ResumableDraft]:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        rows = cursor.execute(
            """
            SELECT DraftId, FleetCode, ChecklistVersionId, VersionCode, RepairCaseId, IsInitialCaseDraft, CreatedAt
            FROM dbo.vw_ResumableInspectionDraft WHERE FleetCode = ?
            ORDER BY CreatedAt DESC, DraftId DESC;
            """,
            fleet_code.strip(),
        ).fetchall()
    return [ResumableDraft.from_row(row) for row in rows]


@app.get("/inspection-drafts/{draft_id}")
def get_inspection_draft(draft_id: UUID) -> ResumedDraft:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        row = cursor.execute(
            """
            SELECT DraftId, FleetCode, ChecklistVersionId, VersionCode, RepairCaseId, IsInitialCaseDraft, CreatedAt, Revision, Payload
            FROM dbo.vw_ResumableInspectionDraft WHERE DraftId = ?;
            """,
            str(draft_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Draft is no longer available. Find drafts again to refresh the list.")
    return ResumedDraft.from_row(
        row, revision=bytes(row.Revision).hex(), contents=DraftContents.model_validate_json(row.Payload)
    )


def authorize_staff_draft(cursor, draft_id: UUID):
    actor = current_staff.get()
    if actor is None:
        return
    row = cursor.execute(
        "SELECT RepairCaseId, IsInitialCaseDraft FROM dbo.InspectionDraft WHERE DraftId = ?;", str(draft_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Inspection draft not found.")
    if actor.role == "technician" and row.IsInitialCaseDraft and row.RepairCaseId:
        require_technician_assignment(cursor, row.RepairCaseId)
    elif actor.role != "quality-control" or row.IsInitialCaseDraft or not row.RepairCaseId:
        raise HTTPException(status_code=403, detail="This inspection draft belongs to another workshop role.")


@app.patch("/inspection-drafts/{draft_id}")
def update_inspection_draft(draft_id: UUID, draft: UpdateInspectionDraft):
    payload = draft.model_dump_json(exclude={"revision", "repairContext"}, exclude_none=True)
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            authorize_staff_draft(cursor, draft_id)
            row = cursor.execute(
                """
                SET NOCOUNT ON;
                DECLARE @Updated TABLE (Revision binary(8) NOT NULL);
                UPDATE d
                SET Payload = JSON_MODIFY(?, '$.repairContext', JSON_VALUE(d.Payload, '$.repairContext'))
                OUTPUT inserted.Revision INTO @Updated (Revision)
                FROM dbo.InspectionDraft AS d
                WHERE d.DraftId = CONVERT(uniqueidentifier, ?) AND d.Revision = ?
                  AND EXISTS (SELECT 1 FROM dbo.vw_ResumableInspectionDraft AS available
                              WHERE available.DraftId = d.DraftId);
                SELECT Revision FROM @Updated;
                """,
                payload,
                str(draft_id),
                pyodbc.Binary(bytes.fromhex(draft.revision)),
            ).fetchone()

        if row is None:
            raise HTTPException(status_code=409, detail="Draft is missing or its revision has changed")
        db.commit()

    return {"draft_id": str(draft_id), "revision": bytes(row.Revision).hex()}


class FinalizeInspectionDraft(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-fA-F]{16}$")
    request_id: UUID
    targeted_failure: bool = False


class FinalizedInspection(BaseModel):
    has_advisories: bool = False
    is_scoped_reinspection: bool = False
    request_id: UUID
    draft_id: UUID
    inspection_id: int
    outcome: Literal["PASS", "FAIL"]
    repair_case_id: int | None


FINALIZATION_ERRORS = {
    51005: (422, "Draft, revision, and request ID are required"),
    51006: (503, "Finalization is busy. Retry with the same request ID"),
    51007: (409, "This request ID belongs to a different draft or revision"),
    51008: (409, "The draft is missing or has changed. Reload it"),
    51009: (409, "This draft has already been finalized"),
    51010: (409, "The repair case is closed"),
    51011: (422, "Provide verification notes of 1 to 500 characters"),
    51012: (422, "Provide valid checklist answers without duplicates"),
    51020: (422, "Complete all checklist items before recording a pass"),
    51021: (422, "Record at least one failure with a finding in this repair case"),
    51022: (422, "Verify every previously failed item before closing the case"),
    51023: (409, "Failed QC is awaiting supervisor reassignment."),
    51013: (422, "Each failed item needs a valid defect. Defects must reference failed items"),
    51016: (409, "The current repair proposal needs customer approval before reinspection."),
    51017: (409, "The case already has an initial inspection."),
    51018: (409, "Log a repair attempt for the current proposal before reinspection."),
    51019: (409, "This draft no longer matches the approved repair. Save and close it, then start a new reinspection."),
}


@app.post("/inspection-drafts/{draft_id}/finalize")
def finalize_inspection_draft(
    draft_id: UUID, draft: FinalizeInspectionDraft
) -> FinalizedInspection:
    try:
        # The stored procedure owns the transaction and commits before returning.
        with closing(connect_to_database(autocommit=True)) as db:
            with closing(db.cursor()) as cursor:
                authorize_staff_draft(cursor, draft_id)
                statement = "EXEC dbo.FinalizeInspection @DraftId = ?, @ExpectedRevision = ?, @RequestId = ?"
                if draft.targeted_failure:
                    statement += ", @TargetedFailure = 1"
                row = cursor.execute(
                    statement,
                    str(draft_id),
                    pyodbc.Binary(bytes.fromhex(draft.revision)),
                    str(draft.request_id),
                ).fetchone()
    except pyodbc.Error as exc:
        for number in re.findall(r"\((\d+)\)", str(exc)):
            if draft.targeted_failure and number in {"8144", "8145"}:
                raise HTTPException(status_code=409, detail="Recording a targeted failure requires database migration 25.") from exc
            if error := FINALIZATION_ERRORS.get(int(number)):
                status, detail = error
                raise HTTPException(status_code=status, detail=detail) from exc
        raise

    return FinalizedInspection(
        has_advisories=bool(getattr(row, 'HasAdvisories', False)),
        is_scoped_reinspection=bool(getattr(row, 'IsScopedReinspection', False)),
        request_id=row.RequestId,
        draft_id=row.DraftId,
        inspection_id=row.InspectionId,
        outcome=row.Outcome,
        repair_case_id=row.RepairCaseId,
    )


@app.post("/inspection-drafts/{draft_id}/finalize-targeted-failure")
def finalize_targeted_failure(draft_id: UUID, draft: FinalizeInspectionDraft) -> FinalizedInspection:
    return finalize_inspection_draft(draft_id, draft.model_copy(update={"targeted_failure": True}))


class RepairCaseSummary(BaseModel):
    repair_case_id: int
    vehicle_id: int
    fleet_code: str
    initial_inspection_id: int | None
    opened_at: datetime


class OpenRepairCase(RepairCaseSummary):
    status: Literal["OPEN"] = "OPEN"


@app.get("/repair-cases/open")
def list_open_repair_cases() -> list[OpenRepairCase]:
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            rows = cursor.execute(
                """
                SELECT rc.RepairCaseId, rc.VehicleId, v.FleetCode,
                       rc.InitialInspectionId, rc.OpenedAt
                FROM dbo.RepairCase AS rc
                JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
                WHERE rc.ClosedAt IS NULL
                ORDER BY rc.OpenedAt DESC, rc.RepairCaseId DESC;
                """
            ).fetchall()

    return [
        OpenRepairCase(
            repair_case_id=row.RepairCaseId,
            vehicle_id=row.VehicleId,
            fleet_code=row.FleetCode,
            initial_inspection_id=row.InitialInspectionId,
            opened_at=row.OpenedAt,
        )
        for row in rows
    ]


class CaseIntake(BaseModel):
    customer_concern: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None = None
    customer_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    customer_phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    customer_email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None
    odometer_km: int | None = Field(default=None, ge=0, le=9999999)
    odometer_miles: int | None = Field(default=None, ge=0, le=9999999)

    @model_validator(mode="after")
    def validate_odometer_unit(self):
        if self.odometer_km is not None and self.odometer_miles is not None:
            raise ValueError("Provide an odometer reading in either kilometres or miles.")
        return self


class OpenCaseRequest(CaseIntake):
    vehicle_id: int = Field(gt=0, le=2147483647)


class OpenedCase(OpenRepairCase):
    created: bool


@app.post("/repair-cases", status_code=201)
def open_repair_case(payload: OpenCaseRequest) -> OpenedCase:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        vehicle = cursor.execute(
            "SELECT FleetCode FROM dbo.Vehicle WITH (UPDLOCK, HOLDLOCK) WHERE VehicleId = ?;",
            payload.vehicle_id,
        ).fetchone()
        if vehicle is None:
            raise HTTPException(status_code=404, detail="Vehicle not found")
        case = cursor.execute(
            """
            SELECT TOP (1) RepairCaseId, InitialInspectionId, OpenedAt
            FROM dbo.RepairCase WHERE VehicleId = ? AND ClosedAt IS NULL
            ORDER BY OpenedAt DESC, RepairCaseId DESC;
            """,
            payload.vehicle_id,
        ).fetchone()
        created = case is None
        if created:
            case = cursor.execute(
                """
                INSERT dbo.RepairCase (
                    VehicleId, InitialOutcome, CustomerConcern, CustomerName, CustomerPhone, CustomerEmail,
                    OdometerKm, OdometerMiles
                )
                OUTPUT inserted.RepairCaseId, inserted.InitialInspectionId, inserted.OpenedAt
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?);
                """,
                payload.vehicle_id, payload.customer_concern, payload.customer_name, payload.customer_phone,
                payload.customer_email, payload.odometer_km, payload.odometer_miles,
            ).fetchone()
        else:
            # Resuming an open case is also an intake save: do not discard details
            # entered on the start form just because the case already exists.
            cursor.execute(
                """
                UPDATE dbo.RepairCase
                SET CustomerConcern = COALESCE(?, CustomerConcern),
                    CustomerName = COALESCE(?, CustomerName),
                    CustomerPhone = COALESCE(?, CustomerPhone),
                    CustomerEmail = COALESCE(?, CustomerEmail),
                    OdometerKm = CASE WHEN ? IS NOT NULL THEN ?
                                      WHEN ? IS NOT NULL THEN NULL ELSE OdometerKm END,
                    OdometerMiles = CASE WHEN ? IS NOT NULL THEN ?
                                         WHEN ? IS NOT NULL THEN NULL ELSE OdometerMiles END
                WHERE RepairCaseId = ?;
                """,
                payload.customer_concern, payload.customer_name, payload.customer_phone, payload.customer_email,
                payload.odometer_km, payload.odometer_km, payload.odometer_miles,
                payload.odometer_miles, payload.odometer_miles, payload.odometer_km,
                case.RepairCaseId,
            )
        db.commit()
    return OpenedCase(
        created=created,
        repair_case_id=case.RepairCaseId, vehicle_id=payload.vehicle_id,
        fleet_code=vehicle.FleetCode, initial_inspection_id=case.InitialInspectionId,
        opened_at=case.OpenedAt,
    )


def initial_checklist_version(cursor, repair_case_id: int) -> int:
    row = cursor.execute(
        """
        SELECT draft.ChecklistVersionId, active.ChecklistVersionId,
               v.Make, v.Model, v.ModelYear, v.Powertrain
        FROM dbo.RepairCase AS rc
        JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
        LEFT JOIN dbo.ActivePowertrainChecklist AS active ON active.Powertrain = v.Powertrain
        LEFT JOIN dbo.vw_ResumableInspectionDraft AS draft
            ON draft.RepairCaseId = rc.RepairCaseId AND draft.IsInitialCaseDraft = 1
        WHERE rc.RepairCaseId = ?;
        """,
        repair_case_id,
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Repair case not found.")
    draft_version, default_version, make, model, year, powertrain = row
    if draft_version is not None:
        return draft_version

    try:
        catalog = json.loads(getenv("INSPECTION_MODEL_CHECKLISTS", "{}"))
        key = "|".join(str(part).strip().upper() for part in (make, model, year, powertrain))
        version_code = catalog.get(key)
        if version_code is not None and (not isinstance(version_code, str) or not version_code.strip()):
            raise ValueError("Checklist version must be a nonempty string.")
    except (ValueError, AttributeError) as error:
        raise HTTPException(status_code=503, detail="Vehicle-specific checklist configuration is invalid.") from error
    if version_code:
        version = cursor.execute(
            "SELECT ChecklistVersionId FROM dbo.ChecklistVersion WHERE VersionCode = ?;",
            version_code.strip(),
        ).fetchone()
        if version is None:
            raise HTTPException(status_code=409, detail="The configured vehicle-specific checklist was not found.")
        return version[0]
    if default_version is None:
        raise HTTPException(status_code=409, detail="Set the vehicle's powertrain and ensure it has an active checklist.")
    return default_version


@app.get("/repair-cases/{repair_case_id}/initial-checklist")
def get_case_initial_checklist(repair_case_id: Annotated[int, Path(gt=0, le=2147483647)]):
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        version_id = initial_checklist_version(cursor, repair_case_id)
    return load_checklist(version_id=version_id)


@app.post("/repair-cases/{repair_case_id}/initial-inspection-drafts", status_code=201)
def create_case_initial_draft(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
) -> ResumedDraft:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        case = lock_repair_case(cursor, repair_case_id)
        require_open_repair_case(case)
        if case.InitialInspectionId is not None:
            raise HTTPException(status_code=409, detail="The case already has an initial inspection.")
        draft = cursor.execute(
            """
            SELECT DraftId, FleetCode, ChecklistVersionId, VersionCode, RepairCaseId,
                   IsInitialCaseDraft, CreatedAt, Revision, Payload
            FROM dbo.vw_ResumableInspectionDraft
            WHERE RepairCaseId = ? AND IsInitialCaseDraft = 1;
            """,
            repair_case_id,
        ).fetchone()
        if draft is None:
            version_id = initial_checklist_version(cursor, repair_case_id)
            created = cursor.execute(
                """
                INSERT dbo.InspectionDraft (VehicleId, ChecklistVersionId, RepairCaseId, IsInitialCaseDraft)
                OUTPUT inserted.DraftId
                SELECT VehicleId, ?, RepairCaseId, 1
                FROM dbo.RepairCase WHERE RepairCaseId = ?;
                """,
                version_id, repair_case_id,
            ).fetchone()
            draft = cursor.execute(
                """
                SELECT DraftId, FleetCode, ChecklistVersionId, VersionCode, RepairCaseId,
                       IsInitialCaseDraft, CreatedAt, Revision, Payload
                FROM dbo.vw_ResumableInspectionDraft WHERE DraftId = ?;
                """,
                str(created.DraftId),
            ).fetchone()
        db.commit()
    return ResumedDraft.from_row(
        draft, revision=bytes(draft.Revision).hex(), contents=DraftContents.model_validate_json(draft.Payload)
    )


class ReinspectionDraft(BaseModel):
    contents: DraftContents
    draft_id: UUID
    revision: str
    repair_case_id: int
    vehicle_id: int
    checklist_version_id: int


@app.post("/repair-cases/{repair_case_id}/reinspection-drafts", status_code=201)
def create_reinspection_draft(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    resume: bool = False,
) -> ReinspectionDraft:
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            require_open_repair_case(lock_repair_case(cursor, repair_case_id))
            require_qc_handoff_complete(cursor, repair_case_id)
            approval = require_approved_proposal(cursor, repair_case_id, "reinspection")
            repair = latest_repair_for_approval(cursor, repair_case_id, approval.RepairApprovalId)
            if repair is None:
                raise HTTPException(status_code=409, detail="Log a repair attempt for the current proposal before reinspection.")
            if resume:
                existing = cursor.execute(
                    """
                    SELECT TOP (1) d.DraftId, d.Revision, d.VehicleId, d.ChecklistVersionId, d.Payload
                    FROM dbo.InspectionDraft AS d
                    JOIN dbo.vw_ResumableInspectionDraft AS available ON available.DraftId = d.DraftId
                    WHERE d.RepairCaseId = ? AND d.IsInitialCaseDraft = 0
                      AND d.VerifiedApprovalId = ? AND d.VerifiedRepairId = ?
                    ORDER BY d.CreatedAt DESC, d.DraftId DESC;
                    """,
                    repair_case_id, approval.RepairApprovalId, repair.RepairAttemptId,
                ).fetchone()
                if existing:
                    return ReinspectionDraft(
                        contents=DraftContents.model_validate_json(existing.Payload),
                        draft_id=existing.DraftId, revision=bytes(existing.Revision).hex(),
                        repair_case_id=repair_case_id, vehicle_id=existing.VehicleId,
                        checklist_version_id=existing.ChecklistVersionId,
                    )
            contents = DraftContents(repairContext=repair.WorkDescription)
            row = cursor.execute(
                """
                SET NOCOUNT ON;
                INSERT dbo.InspectionDraft
                    (VehicleId, ChecklistVersionId, RepairCaseId, Payload, VerifiedApprovalId, VerifiedRepairId)
                OUTPUT inserted.DraftId, inserted.Revision, inserted.VehicleId,
                       inserted.ChecklistVersionId
                SELECT rc.VehicleId, i.ChecklistVersionId, rc.RepairCaseId, ?, ?, ?
                FROM dbo.RepairCase AS rc WITH (HOLDLOCK)
                JOIN dbo.Inspection AS i ON i.InspectionId = rc.InitialInspectionId
                WHERE rc.RepairCaseId = ? AND rc.ClosedAt IS NULL;
                """,
                contents.model_dump_json(exclude_none=True), approval.RepairApprovalId, repair.RepairAttemptId, repair_case_id,
            ).fetchone()

        if row is None:
            raise HTTPException(status_code=409, detail="Repair case is missing or closed")
        db.commit()

    return ReinspectionDraft(
        contents=contents,
        draft_id=row.DraftId,
        revision=bytes(row.Revision).hex(),
        repair_case_id=repair_case_id,
        vehicle_id=row.VehicleId,
        checklist_version_id=row.ChecklistVersionId,
    )


CaseStage = Literal[
    "AWAITING_INSPECTION", "NEEDS_PROPOSAL", "AWAITING_APPROVAL", "DECLINED",
    "READY_FOR_REPAIR", "READY_FOR_REINSPECTION", "REINSPECTION_FAILED", "AWAITING_REASSIGNMENT", "WITHDRAWN", "CLOSED_WITHOUT_REPAIR", "CANCELLED", "CLOSED",
]


class CaseProgress(BaseModel):
    stage: CaseStage
    stage_started_at: AwareDatetime


class CaseHandoff(BaseModel):
    owner_role: Literal["SERVICE_ADVISOR", "SUPERVISOR", "TECHNICIAN", "QUALITY_CONTROL", "NONE"]
    owner_name: str | None = None
    next_action: str
    blocking_reason: str


def case_handoff(progress: CaseProgress, status: str, assignee=None, staff_role=None) -> CaseHandoff:
    stage = progress.stage
    owner_name = assignee if staff_role == "TECHNICIAN" else None
    if status == "CLOSED" or stage in ("CANCELLED", "CLOSED_WITHOUT_REPAIR", "CLOSED"):
        return CaseHandoff(owner_role="NONE", next_action="No further action", blocking_reason="Case is closed")
    if stage == "AWAITING_INSPECTION":
        role = "TECHNICIAN" if owner_name else "SUPERVISOR"
        action = "Complete the initial inspection" if owner_name else "Assign a technician for inspection"
        blocker = "Initial inspection is not complete"
    elif stage in ("NEEDS_PROPOSAL", "AWAITING_APPROVAL", "DECLINED", "WITHDRAWN"):
        role, owner_name = "SERVICE_ADVISOR", None
        action, blocker = {
            "NEEDS_PROPOSAL": ("Record diagnosis and repair proposal", "Diagnosis and proposal are needed"),
            "AWAITING_APPROVAL": ("Contact customer and record decision", "Waiting for the customer decision"),
            "DECLINED": ("Confirm the next step with the customer", "Customer declined the proposal"),
            "WITHDRAWN": ("Submit a revised repair proposal", "The proposal was withdrawn"),
        }[stage]
    elif stage == "READY_FOR_REPAIR":
        role = "TECHNICIAN" if owner_name else "SUPERVISOR"
        action = "Complete the approved repair" if owner_name else "Assign a technician"
        blocker = "Approved repair is waiting for workshop work" if owner_name else "Technician assignment is needed"
    elif stage == "READY_FOR_REINSPECTION":
        role, owner_name = "QUALITY_CONTROL", None
        action, blocker = "Perform the reinspection", "Repair is recorded; quality verification is due"
    elif stage == "AWAITING_REASSIGNMENT":
        role, owner_name = "SUPERVISOR", None
        action, blocker = "Reassign the case for rework", "Failed QC requires supervisor reassignment"
    elif stage == "REINSPECTION_FAILED":
        role = "TECHNICIAN" if owner_name else "SUPERVISOR"
        action = "Repair the failed QC items" if owner_name else "Assign a technician for rework"
        blocker = "Reinspection failed; further repair is needed"
    else:
        role, owner_name = "SUPERVISOR", None
        action, blocker = "Review the case", "The next workflow step needs review"
    return CaseHandoff(owner_role=role, owner_name=owner_name, next_action=action, blocking_reason=blocker)


def supports_case_cancellation(cursor) -> bool:
    return bool(cursor.execute(
        """
        SELECT CASE WHEN COL_LENGTH(N'dbo.RepairCase', N'CancellationReason') IS NOT NULL
                     AND COL_LENGTH(N'dbo.RepairCase', N'CancelledBy') IS NOT NULL
                    THEN 1 ELSE 0 END;
        """
    ).fetchval())


def failed_verification_time(outcome, inspected_at, verified_repair_id, repair_id, repair_recorded_at):
    if outcome != "FAIL" or inspected_at is None or repair_id is None:
        return None
    matches = verified_repair_id == repair_id if verified_repair_id is not None else (
        repair_recorded_at is not None and repair_recorded_at <= inspected_at
    )
    return inspected_at if matches else None


def case_progress(case, approval_status, requested_at, decided_at, repair_at, failed_at=None,
                  *, awaiting_reassignment=False, reassigned_at=None) -> CaseProgress:
    if case.ClosedAt is not None:
        if case.CancellationReason:
            stage = "CANCELLED"
        else:
            stage = "CLOSED_WITHOUT_REPAIR" if case.CloseoutReason else "CLOSED"
        since = case.ClosedAt
    elif case.InitialInspectionId is None:
        stage, since = "AWAITING_INSPECTION", case.OpenedAt
    elif approval_status is None:
        stage, since = "NEEDS_PROPOSAL", case.InitialInspectedAt
    elif approval_status == "PENDING":
        stage, since = "AWAITING_APPROVAL", requested_at
    elif approval_status == "WITHDRAWN":
        stage, since = "WITHDRAWN", decided_at
    elif approval_status == "DECLINED":
        stage, since = "DECLINED", decided_at
    elif repair_at is None:
        stage, since = "READY_FOR_REPAIR", decided_at
    elif awaiting_reassignment:
        stage, since = "AWAITING_REASSIGNMENT", failed_at
    elif failed_at is not None:
        stage, since = "REINSPECTION_FAILED", max(failed_at, reassigned_at or failed_at)
    else:
        stage, since = "READY_FOR_REINSPECTION", repair_at
    return CaseProgress(stage=stage, stage_started_at=max(case.OpenedAt, since or case.OpenedAt).replace(tzinfo=timezone.utc))


def progress_from_search_row(row) -> CaseProgress:
    failed_at = failed_verification_time(row.VerificationOutcome, row.VerificationAt, row.VerifiedRepairId,
                                         row.RepairAttemptId, row.LatestRepairAt)
    reassigned = row.Assignee and row.HandoffInspectionId == row.VerificationInspectionId
    return case_progress(row, row.ApprovalStatus, row.RequestedAt, row.DecidedAt, row.LatestRepairAt, failed_at,
                         awaiting_reassignment=bool(failed_at and row.HandoffEnabled and not reassigned),
                         reassigned_at=row.AssignmentAt if reassigned else None)


class RepairCaseSearchResult(RepairCaseSummary):
    status: Literal["OPEN", "CLOSED"]
    closed_at: datetime | None
    progress: CaseProgress
    handoff: CaseHandoff
    is_test_data: bool


def read_case_rows(cursor, where: str, values=(), date_column="rc.OpenedAt"):
    cancellation_reason = "rc.CancellationReason" if supports_case_cancellation(cursor) else "NULL"
    enabled = workshop_availability_available(cursor)
    owner = """
        OUTER APPLY (
            SELECT TOP (1) WorkshopCaseEventId, Assignee, StaffRole, InspectionId, RecordedAt
            FROM dbo.WorkshopCaseEvent WHERE RepairCaseId = rc.RepairCaseId AND EventKind = 'ASSIGNMENT'
            ORDER BY WorkshopCaseEventId DESC
        ) AS owner
    """ if enabled else """
        OUTER APPLY (SELECT NULL AS WorkshopCaseEventId, NULL AS Assignee, NULL AS StaffRole,
            NULL AS InspectionId, NULL AS RecordedAt) AS owner
    """
    return cursor.execute(
        f"""
        SELECT rc.RepairCaseId, rc.VehicleId, v.FleetCode, rc.IsTestData,
               rc.InitialInspectionId, rc.OpenedAt, rc.ClosedAt, rc.CloseoutReason, {cancellation_reason} AS CancellationReason,
               initial.InspectedAt AS InitialInspectedAt,
               proposal.ApprovalStatus, proposal.RequestedAt, proposal.DecidedAt,
               repair.RepairAttemptId, repair.RecordedAt AS LatestRepairAt,
               review.Outcome AS VerificationOutcome, review.InspectedAt AS VerificationAt, review.VerifiedRepairId,
               review.InspectionId AS VerificationInspectionId, owner.Assignee, owner.StaffRole,
               owner.WorkshopCaseEventId AS AssignmentId, owner.InspectionId AS HandoffInspectionId,
               owner.RecordedAt AS AssignmentAt, {int(enabled)} AS HandoffEnabled
        FROM dbo.RepairCase AS rc
        JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
        LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
        OUTER APPLY (
            SELECT TOP (1) RepairApprovalId, ApprovalStatus, RequestedAt, DecidedAt
            FROM dbo.RepairApproval WHERE RepairCaseId = rc.RepairCaseId
            ORDER BY RequestedAt DESC, RepairApprovalId DESC
        ) AS proposal
        OUTER APPLY (
            SELECT TOP (1) RepairAttemptId, RecordedAt FROM dbo.RepairAttempt
            WHERE RepairCaseId = rc.RepairCaseId AND RepairApprovalId = proposal.RepairApprovalId
            ORDER BY PerformedAt DESC, RepairAttemptId DESC
        ) AS repair
        OUTER APPLY (
            SELECT TOP (1) i.InspectionId, i.Outcome, i.InspectedAt, attempt.VerifiedRepairId
            FROM dbo.ReinspectionAttempt AS attempt
            JOIN dbo.Inspection AS i ON i.InspectionId = attempt.InspectionId
            WHERE attempt.RepairCaseId = rc.RepairCaseId
            ORDER BY attempt.AttemptNumber DESC
        ) AS review
        {owner}
        WHERE {where}
        ORDER BY {date_column} DESC, rc.RepairCaseId DESC;
        """,
        *values,
    ).fetchall()



@app.get("/repair-cases")
def search_repair_cases(
    fleet_code: Annotated[str | None, Query(min_length=1, max_length=30)] = None,
    status: Literal["OPEN", "CLOSED"] | None = None,
    vehicle_identifier: Annotated[str | None, Query(min_length=1, max_length=30)] = None,
) -> list[RepairCaseSearchResult]:
    conditions = []
    values = []
    if code := (fleet_code or "").strip():
        conditions.append("v.FleetCode = ?")
        values.append(code)
    if identifier := (vehicle_identifier or "").strip().upper():
        conditions.append("(v.VIN = ? OR v.FleetCode = ?)")
        values.extend((identifier, identifier))
    if status == "OPEN":
        conditions.append("rc.ClosedAt IS NULL")
    elif status == "CLOSED":
        conditions.append("rc.ClosedAt IS NOT NULL")
    if not conditions:
        raise HTTPException(status_code=422, detail="Provide a VIN, fleet code, or case status.")

    where = " AND ".join(conditions)
    date_column = "rc.ClosedAt" if status == "CLOSED" else "rc.OpenedAt"
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        rows = read_case_rows(cursor, where, values, date_column)
    return [
        RepairCaseSearchResult(
            repair_case_id=row.RepairCaseId,
            vehicle_id=row.VehicleId,
            fleet_code=row.FleetCode,
            initial_inspection_id=row.InitialInspectionId,
            opened_at=row.OpenedAt,
            status="CLOSED" if row.ClosedAt is not None else "OPEN",
            closed_at=row.ClosedAt,
            progress=progress_from_search_row(row),
            handoff=case_handoff(
                progress_from_search_row(row), "CLOSED" if row.ClosedAt is not None else "OPEN",
                row.Assignee, row.StaffRole,
            ),
            is_test_data=bool(row.IsTestData),
        )
        for row in rows
    ]


RepairOutcome = Literal["RESOLVED", "UNRESOLVED", "INCONCLUSIVE"]


ShortApprovalText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ApprovalScopeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
ApprovalDecision = Literal["APPROVED", "DECLINED"]


class RequestRepairApproval(BaseModel):
    diagnosis: ApprovalScopeText
    proposed_repair: ApprovalScopeText
    requested_by: ShortApprovalText


class RecordRepairDecision(BaseModel):
    status: ApprovalDecision
    recorded_by: ShortApprovalText
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None


class RepairApproval(RequestRepairApproval):
    repair_approval_id: int
    status: Literal["PENDING", "APPROVED", "DECLINED", "WITHDRAWN"]
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None
    recorded_by: str | None
    note: str | None


class RecordAdvisorFollowUp(BaseModel):
    contact_method: Literal["PHONE", "EMAIL", "SMS", "OTHER"]
    contact_result: Literal["REACHED", "NO_ANSWER", "LEFT_MESSAGE", "OTHER"]
    recorded_by: ShortApprovalText
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    next_follow_up_at: AwareDatetime | None = None


class AdvisorFollowUpContact(RecordAdvisorFollowUp):
    follow_up_contact_id: int
    repair_approval_id: int
    contacted_at: AwareDatetime


def advisor_follow_up_contact_from_row(row) -> AdvisorFollowUpContact:
    return AdvisorFollowUpContact(
        follow_up_contact_id=row.FollowUpContactId,
        repair_approval_id=row.RepairApprovalId,
        contacted_at=row.ContactedAt.replace(tzinfo=timezone.utc),
        contact_method=row.ContactMethod,
        contact_result=row.ContactResult,
        recorded_by=row.RecordedBy,
        note=row.Note,
        next_follow_up_at=row.NextFollowUpAt.replace(tzinfo=timezone.utc) if row.NextFollowUpAt else None,
    )


def require_technician_assignment(cursor, repair_case_id: int):
    actor = current_staff.get()
    if actor is None or actor.role != "technician":
        return
    if not workshop_tracking_available(cursor):
        raise HTTPException(status_code=409, detail="Workshop tracking requires database migration 27.")
    owner = cursor.execute(
        """
        SELECT TOP (1) Assignee, StaffRole FROM dbo.WorkshopCaseEvent
        WHERE RepairCaseId = ? AND EventKind = 'ASSIGNMENT'
        ORDER BY WorkshopCaseEventId DESC;
        """,
        repair_case_id,
    ).fetchone()
    if (owner is None or owner.StaffRole != "TECHNICIAN" or not owner.Assignee
            or owner.Assignee.strip().casefold() != actor.name.strip().casefold()):
        raise HTTPException(status_code=403, detail="This case is not assigned to you.")


def lock_repair_case(cursor, repair_case_id: int):
    case = cursor.execute(
        "SELECT ClosedAt, InitialInspectionId FROM dbo.RepairCase WITH (UPDLOCK, HOLDLOCK) WHERE RepairCaseId = ?;",
        repair_case_id,
    ).fetchone()
    if case is None:
        raise HTTPException(status_code=404, detail="Repair case not found")
    require_technician_assignment(cursor, repair_case_id)
    return case


def require_open_repair_case(case):
    if case.ClosedAt is not None:
        raise HTTPException(status_code=409, detail="Repair case is closed")


def read_repair_approvals(cursor, repair_case_id: int, *, latest_only: bool = False):
    limit = "TOP (1)" if latest_only else ""
    return cursor.execute(
        f"""
        SELECT {limit} RepairApprovalId, Diagnosis, ProposedRepair, RequestedBy, RequestedAt,
            ApprovalStatus, DecidedAt, DecisionRecordedBy, DecisionNote
        FROM dbo.RepairApproval
        WHERE RepairCaseId = ?
        ORDER BY RequestedAt DESC, RepairApprovalId DESC;
        """,
        repair_case_id,
    )


def require_approved_proposal(cursor, repair_case_id: int, action: str):
    approval = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
    if approval is None or approval.ApprovalStatus != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail=f"The current repair proposal needs customer approval before {action}.",
        )
    return approval


def latest_repair_for_approval(cursor, repair_case_id: int, approval_id: int):
    return cursor.execute(
        """
        SELECT TOP (1) RepairAttemptId, WorkDescription, Outcome, RecordedAt
        FROM dbo.RepairAttempt
        WHERE RepairCaseId = ? AND RepairApprovalId = ?
        ORDER BY PerformedAt DESC, RepairAttemptId DESC;
        """,
        repair_case_id, approval_id,
    ).fetchone()


def repair_approval_from_row(row) -> RepairApproval:
    return RepairApproval(
        repair_approval_id=row.RepairApprovalId,
        diagnosis=row.Diagnosis,
        proposed_repair=row.ProposedRepair,
        requested_by=row.RequestedBy,
        requested_at=row.RequestedAt.replace(tzinfo=timezone.utc),
        status=row.ApprovalStatus,
        decided_at=row.DecidedAt.replace(tzinfo=timezone.utc) if row.DecidedAt is not None else None,
        recorded_by=row.DecisionRecordedBy,
        note=row.DecisionNote,
    )


@app.get("/repair-cases/{repair_case_id}/approvals")
def list_repair_approvals(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
) -> list[RepairApproval]:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        return [repair_approval_from_row(row) for row in read_repair_approvals(cursor, repair_case_id).fetchall()]


@app.post("/repair-cases/{repair_case_id}/approvals", status_code=201)
def request_repair_approval(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    proposal: RequestRepairApproval,
) -> RepairApproval:
    proposal = audited(proposal, "requested_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        case = lock_repair_case(cursor, repair_case_id)
        require_open_repair_case(case)
        require_qc_handoff_complete(cursor, repair_case_id)
        if case.InitialInspectionId is None:
            raise HTTPException(status_code=409, detail="Complete the initial inspection before recording a repair proposal.")
        latest = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
        if latest is not None and latest.ApprovalStatus == "PENDING":
            saved = repair_approval_from_row(latest)
            if (saved.diagnosis, saved.proposed_repair, saved.requested_by) == (
                proposal.diagnosis, proposal.proposed_repair, proposal.requested_by
            ):
                return saved
            raise HTTPException(status_code=409, detail="Record the current proposal's decision before sending a revised proposal.")
        cursor.execute(
            """
            INSERT dbo.RepairApproval (RepairCaseId, Diagnosis, ProposedRepair, RequestedBy)
            VALUES (?, ?, ?, ?);
            """,
            repair_case_id, proposal.diagnosis, proposal.proposed_repair, proposal.requested_by,
        )
        saved = repair_approval_from_row(read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone())
        db.commit()
        return saved


@app.patch("/repair-cases/{repair_case_id}/approvals/{approval_id}")
def record_repair_decision(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    approval_id: Annotated[int, Path(gt=0, le=2147483647)],
    decision: RecordRepairDecision,
) -> RepairApproval:
    decision = audited(decision, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        capacity_enabled = workshop_availability_available(cursor)
        if capacity_enabled:
            lock_technician_capacity(cursor)
        require_open_repair_case(lock_repair_case(cursor, repair_case_id))
        latest = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
        if latest is None or latest.RepairApprovalId != approval_id:
            raise HTTPException(status_code=409, detail="This is no longer the current repair proposal. Refresh case history.")
        if latest.ApprovalStatus != "PENDING":
            saved = repair_approval_from_row(latest)
            if (saved.status, saved.recorded_by, saved.note) == (decision.status, decision.recorded_by, decision.note):
                return saved
            raise HTTPException(status_code=409, detail="A decision has already been recorded for this proposal.")
        cursor.execute(
            """
            UPDATE dbo.RepairApproval
            SET ApprovalStatus = ?, DecidedAt = SYSUTCDATETIME(),
                DecisionRecordedBy = ?, DecisionNote = ?
            WHERE RepairCaseId = ? AND RepairApprovalId = ?;
            """,
            decision.status, decision.recorded_by, decision.note, repair_case_id, approval_id,
        )
        saved = repair_approval_from_row(read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone())
        if capacity_enabled:
            release_unavailable_owner(cursor, repair_case_id, decision.recorded_by)
        db.commit()
        return saved


class WithdrawRepairProposal(BaseModel):
    recorded_by: ShortApprovalText
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


@app.post("/repair-cases/{repair_case_id}/approvals/{approval_id}/withdraw")
def withdraw_repair_proposal(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    approval_id: Annotated[int, Path(gt=0, le=2147483647)],
    withdrawal: WithdrawRepairProposal,
) -> RepairApproval:
    withdrawal = audited(withdrawal, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        capacity_enabled = workshop_availability_available(cursor)
        if capacity_enabled:
            lock_technician_capacity(cursor)
        require_open_repair_case(lock_repair_case(cursor, repair_case_id))
        latest = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
        if latest is None or latest.RepairApprovalId != approval_id:
            raise HTTPException(status_code=409, detail="This is no longer the current repair proposal. Refresh case history.")
        if latest.ApprovalStatus == "WITHDRAWN" and (
            latest.DecisionRecordedBy, latest.DecisionNote
        ) == (withdrawal.recorded_by, withdrawal.note):
            return repair_approval_from_row(latest)
        if latest.ApprovalStatus != "PENDING":
            raise HTTPException(status_code=409, detail="Only a pending proposal can be withdrawn.")
        cursor.execute(
            """
            UPDATE dbo.RepairApproval
            SET ApprovalStatus = 'WITHDRAWN', DecidedAt = SYSUTCDATETIME(),
                DecisionRecordedBy = ?, DecisionNote = ?
            WHERE RepairCaseId = ? AND RepairApprovalId = ?;
            """,
            withdrawal.recorded_by, withdrawal.note, repair_case_id, approval_id,
        )
        saved = repair_approval_from_row(read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone())
        if capacity_enabled:
            release_unavailable_owner(cursor, repair_case_id, withdrawal.recorded_by)
        db.commit()
        return saved


class CloseCaseWithoutRepair(BaseModel):
    recorded_by: ShortApprovalText
    reason: ApprovalScopeText


@app.post("/repair-cases/{repair_case_id}/close-without-repair")
def close_case_without_repair(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    closeout: CloseCaseWithoutRepair,
) -> CloseCaseWithoutRepair:
    closeout = audited(closeout, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        case = cursor.execute(
            """
            SELECT ClosedAt, CloseoutReason, ClosedBy FROM dbo.RepairCase WITH (UPDLOCK, HOLDLOCK)
            WHERE RepairCaseId = ?;
            """,
            repair_case_id,
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Repair case not found")
        if case.ClosedAt is not None and (case.CloseoutReason, case.ClosedBy) == (closeout.reason, closeout.recorded_by):
            return closeout
        require_open_repair_case(case)
        latest = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
        if latest is None or latest.ApprovalStatus != "DECLINED":
            raise HTTPException(status_code=409, detail="Record a customer decline before closing without repair.")
        cursor.execute(
            """
            UPDATE dbo.RepairCase
            SET ClosedAt = SYSUTCDATETIME(), CloseoutReason = ?, ClosedBy = ?
            WHERE RepairCaseId = ?;
            """,
            closeout.reason, closeout.recorded_by, repair_case_id,
        )
        db.commit()
        return closeout


@app.post("/repair-cases/{repair_case_id}/cancel")
def cancel_repair_case(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    cancellation: CloseCaseWithoutRepair,
) -> CloseCaseWithoutRepair:
    cancellation = audited(cancellation, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if not supports_case_cancellation(cursor):
            raise HTTPException(status_code=503, detail="Case cancellation requires database migration 23.")
        case = cursor.execute(
            """
            SELECT ClosedAt, CancellationReason, CancelledBy
            FROM dbo.RepairCase WITH (UPDLOCK, HOLDLOCK)
            WHERE RepairCaseId = ?;
            """,
            repair_case_id,
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Repair case not found")
        if case.ClosedAt is not None and (case.CancellationReason, case.CancelledBy) == (
            cancellation.reason, cancellation.recorded_by
        ):
            return cancellation
        require_open_repair_case(case)
        cursor.execute(
            """
            UPDATE dbo.RepairCase
            SET ClosedAt = SYSUTCDATETIME(), CancellationReason = ?, CancelledBy = ?
            WHERE RepairCaseId = ?;
            """,
            cancellation.reason, cancellation.recorded_by, repair_case_id,
        )
        db.commit()
        return cancellation


class RepairAttemptWorkItemInput(BaseModel):
    work_type: Literal["LABOR", "PART"]
    approved_scope: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    part_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    quantity: float | None = Field(default=None, gt=0, le=100000)
    labor_minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def validate_work_item(self):
        if self.work_type == "LABOR" and (self.labor_minutes is None or self.part_code is not None or self.quantity is not None):
            raise ValueError("Labor rows need labor minutes and cannot include a part quantity.")
        if self.work_type == "PART" and (not self.part_code or self.quantity is None or self.labor_minutes is not None):
            raise ValueError("Part rows need a part code and quantity and cannot include labor minutes.")
        return self


class RepairAttemptWorkItem(BaseModel):
    work_item_id: int
    work_type: Literal["LABOR", "PART"]
    approved_scope: str
    description: str
    part_code: str | None = None
    quantity: float | None = None
    labor_minutes: int | None = None


class LogRepairAttempt(BaseModel):
    request_id: UUID
    performed_at: AwareDatetime
    technician: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    work_description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    outcome: RepairOutcome
    work_items: list[RepairAttemptWorkItemInput] = Field(default_factory=list, max_length=10)


class RepairAttemptHistoryItem(BaseModel):
    repair_attempt_id: int
    performed_at: datetime
    technician: str
    work_description: str
    outcome: RepairOutcome
    work_items: list[RepairAttemptWorkItem] = Field(default_factory=list)


def repair_attempt_work_items(cursor, repair_attempt_id: int) -> list[RepairAttemptWorkItem]:
    if cursor.execute("SELECT OBJECT_ID(N'dbo.RepairAttemptWorkItem', N'U');").fetchval() is None:
        return []
    rows = cursor.execute(
        """
        SELECT RepairAttemptWorkItemId, WorkType, ApprovedScope, Description, PartCode, Quantity, LaborMinutes
        FROM dbo.RepairAttemptWorkItem WHERE RepairAttemptId = ? ORDER BY RepairAttemptWorkItemId;
        """,
        repair_attempt_id,
    ).fetchall()
    return [RepairAttemptWorkItem(
        work_item_id=row.RepairAttemptWorkItemId, work_type=row.WorkType,
        approved_scope=row.ApprovedScope, description=row.Description,
        part_code=row.PartCode, quantity=float(row.Quantity) if row.Quantity is not None else None,
        labor_minutes=row.LaborMinutes,
    ) for row in rows]


@app.post("/repair-cases/{repair_case_id}/repair-attempts", status_code=201)
def log_repair_attempt(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    attempt: LogRepairAttempt,
) -> RepairAttemptHistoryItem:
    attempt = audited(attempt, "technician")
    performed_at = attempt.performed_at.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    values = (performed_at, attempt.technician, attempt.work_description, attempt.outcome)
    request_id = str(attempt.request_id)
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        # Hold the case lock through commit so a passing reinspection cannot close it mid-write.
        case = lock_repair_case(cursor, repair_case_id)

        existing = cursor.execute(
            """
            SELECT RepairAttemptId, PerformedAt, Technician, WorkDescription, Outcome
            FROM dbo.RepairAttempt WHERE RepairCaseId = ? AND RequestId = ?;
            """,
            repair_case_id, request_id,
        ).fetchone()
        if attempt.work_items and cursor.execute("SELECT OBJECT_ID(N'dbo.RepairAttemptWorkItem', N'U');").fetchval() is None:
            raise HTTPException(status_code=503, detail="Structured repair details require migration 31.")
        if existing is not None:
            if tuple(existing)[1:] != values:
                raise HTTPException(status_code=409, detail="This request already saved different repair details")
            attempt_id = existing.RepairAttemptId
            saved_items = repair_attempt_work_items(cursor, attempt_id)
            expected_items = [item.model_dump() for item in attempt.work_items]
            actual_items = [{
                "work_type": item.work_type, "approved_scope": item.approved_scope,
                "description": item.description, "part_code": item.part_code,
                "quantity": item.quantity, "labor_minutes": item.labor_minutes,
            } for item in saved_items]
            if expected_items != actual_items:
                raise HTTPException(status_code=409, detail="This request already saved different structured repair details")
        else:
            require_open_repair_case(case)
            require_qc_handoff_complete(cursor, repair_case_id)
            if workshop_availability_available(cursor):
                assignment = latest_workshop_assignment(cursor, repair_case_id)
                if assignment and not assignment.Assignee:
                    raise HTTPException(status_code=409, detail="A supervisor must assign this case before repair.")
            approval = require_approved_proposal(cursor, repair_case_id, "logging repairs")
            if performed_at < approval.DecidedAt:
                raise HTTPException(status_code=422, detail="Repair time must be at or after the recorded customer approval.")
            attempt_id = cursor.execute(
                """
                INSERT dbo.RepairAttempt
                    (RepairCaseId, RequestId, PerformedAt, Technician, WorkDescription, Outcome, RepairApprovalId)
                OUTPUT INSERTED.RepairAttemptId
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                repair_case_id, request_id, *values, approval.RepairApprovalId,
            ).fetchone()[0]
            for item in attempt.work_items:
                cursor.execute(
                    """
                    INSERT dbo.RepairAttemptWorkItem
                        (RepairAttemptId, WorkType, ApprovedScope, Description, PartCode, Quantity, LaborMinutes)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    attempt_id, item.work_type, item.approved_scope, item.description,
                    item.part_code, item.quantity, item.labor_minutes,
                )
        work_items = repair_attempt_work_items(cursor, attempt_id)
        db.commit()
    return RepairAttemptHistoryItem(
        repair_attempt_id=attempt_id,
        performed_at=performed_at.replace(tzinfo=timezone.utc),
        technician=attempt.technician,
        work_description=attempt.work_description,
        outcome=attempt.outcome,
        work_items=work_items,
    )


class InspectionFinding(BaseModel):
    severity: Literal["MINOR", "MAJOR", "CRITICAL"]
    finding: str


class InitialInspectionItem(BaseModel):
    item_code: str
    label: str
    system: str
    outcome: Literal["PASS", "FAIL"]
    findings: list[InspectionFinding] = Field(default_factory=list)


class ReinspectionHistoryItem(BaseModel):
    verified_approval_id: int | None = None
    verified_repair_id: int | None = None
    attempt_number: int
    inspection_id: int
    outcome: Literal["PASS", "FAIL"]
    inspected_at: datetime
    repair_note: str
    unanswered_count: int = 0
    items: list[InitialInspectionItem] = Field(default_factory=list)


class InitialInspectionDetails(BaseModel):
    inspected_at: AwareDatetime
    outcome: Literal["PASS", "FAIL"]
    checklist_title: str
    checklist_version: str
    items: list[InitialInspectionItem]
    unanswered_count: int = 0


DiagnosticText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
DiagnosticTechnician = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class CreateDiagnosticCheck(BaseModel):
    check_performed: DiagnosticText
    observation: DiagnosticText
    technician: DiagnosticTechnician | None = None


class DiagnosticCheck(CreateDiagnosticCheck):
    diagnostic_check_id: int
    recorded_at: AwareDatetime


def diagnostic_check_from_row(row) -> DiagnosticCheck:
    return DiagnosticCheck(
        diagnostic_check_id=row.DiagnosticCheckId,
        check_performed=row.CheckPerformed,
        observation=row.Observation,
        technician=row.Technician,
        recorded_at=row.RecordedAt.replace(tzinfo=timezone.utc),
    )


@app.post("/repair-cases/{repair_case_id}/diagnostic-checks", status_code=201)
def add_diagnostic_check(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    check: CreateDiagnosticCheck,
) -> DiagnosticCheck:
    check = audited(check, "technician")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        case = lock_repair_case(cursor, repair_case_id)
        require_open_repair_case(case)
        failed = cursor.execute(
            "SELECT 1 FROM dbo.Inspection WHERE InspectionId = ? AND Outcome = 'FAIL';",
            case.InitialInspectionId,
        ).fetchval()
        if not failed:
            raise HTTPException(status_code=409, detail="Complete a failed initial inspection before adding diagnostic checks.")
        if cursor.execute("SELECT OBJECT_ID(N'dbo.DiagnosticCheck', N'U');").fetchval() is None:
            raise HTTPException(status_code=503, detail="The diagnostic work log requires database migration 20.")
        row = cursor.execute(
            """
            INSERT dbo.DiagnosticCheck (RepairCaseId, CheckPerformed, Observation, Technician)
            OUTPUT inserted.DiagnosticCheckId, inserted.CheckPerformed, inserted.Observation,
                   inserted.Technician, inserted.RecordedAt
            VALUES (?, ?, ?, ?);
            """,
            repair_case_id, check.check_performed, check.observation, check.technician,
        ).fetchone()
        saved = diagnostic_check_from_row(row)
        db.commit()
        return saved


class AdvisoryFinding(InspectionFinding):
    label: str


class RepairCaseHistory(CaseIntake):
    closeout_reason: str | None = None
    closed_by: str | None = None
    cancellation_reason: str | None = None
    cancelled_by: str | None = None
    retest_without_new_repair: bool = False
    pending_qc_inspection_id: int | None = None
    current_repair_outcome: RepairOutcome | None = None
    advisories: list[AdvisoryFinding] = Field(default_factory=list)
    repair_case_id: int
    vehicle_id: int
    fleet_code: str
    vin: str | None = None
    vehicle_make: str | None = None
    vehicle_model: str | None = None
    vehicle_model_year: int | None = None
    vehicle_powertrain: Powertrain | None = None
    initial_inspection_id: int | None
    status: Literal["OPEN", "CLOSED"]
    is_test_data: bool
    opened_at: datetime
    closed_at: datetime | None
    closing_inspection_id: int | None
    initial_inspection: InitialInspectionDetails | None
    can_reinspect: bool
    progress: CaseProgress
    handoff: CaseHandoff
    previous_failed_item_codes: list[str]
    required_verification_item_codes: list[str] = Field(default_factory=list)
    attempts: list[ReinspectionHistoryItem]
    repair_attempts: list[RepairAttemptHistoryItem]
    diagnostic_checks: list[DiagnosticCheck] | None
    follow_up_contacts: list[AdvisorFollowUpContact] = Field(default_factory=list)


@app.get("/repair-cases/{repair_case_id}")
def get_repair_case_history(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
) -> RepairCaseHistory:
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            cancellation_supported = supports_case_cancellation(cursor)
            cancellation_reason = "rc.CancellationReason" if cancellation_supported else "NULL"
            cancelled_by = "rc.CancelledBy" if cancellation_supported else "NULL"
            rows = cursor.execute(
                f"""
                SELECT rc.RepairCaseId, rc.VehicleId, v.FleetCode, v.VIN, v.Make AS VehicleMake, v.Model AS VehicleModel,
                       v.ModelYear AS VehicleModelYear, v.Powertrain AS VehiclePowertrain,
                       rc.InitialInspectionId, rc.IsTestData,
                       rc.OpenedAt, rc.ClosedAt, rc.ClosingInspectionId, rc.CustomerConcern,
                       rc.CustomerName, rc.CustomerPhone, rc.CustomerEmail, rc.OdometerKm, rc.OdometerMiles,
                       rc.CloseoutReason, rc.ClosedBy, {cancellation_reason} AS CancellationReason, {cancelled_by} AS CancelledBy,
                       initial.InspectedAt AS InitialInspectedAt, initial.Outcome AS InitialOutcome,
                       cv.Title AS ChecklistTitle, cv.VersionCode AS ChecklistVersion,
                       ra.AttemptNumber, ra.RepairNote, ra.VerifiedApprovalId, ra.VerifiedRepairId,
                       i.InspectionId, i.Outcome, i.InspectedAt,
                       checklist.TotalItems, attemptResults.AnsweredItems
                FROM dbo.RepairCase AS rc
                JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
                LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
                LEFT JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = initial.ChecklistVersionId
                LEFT JOIN dbo.ReinspectionAttempt AS ra ON ra.RepairCaseId = rc.RepairCaseId
                LEFT JOIN dbo.Inspection AS i ON i.InspectionId = ra.InspectionId
                OUTER APPLY (
                    SELECT COUNT(*) AS TotalItems FROM dbo.ChecklistItem
                    WHERE ChecklistVersionId = initial.ChecklistVersionId
                ) AS checklist
                OUTER APPLY (
                    SELECT COUNT(*) AS AnsweredItems FROM dbo.ChecklistResult
                    WHERE InspectionId = ra.InspectionId
                ) AS attemptResults
                WHERE rc.RepairCaseId = ?
                ORDER BY ra.AttemptNumber;
                """,
                repair_case_id,
            ).fetchall()

            if not rows:
                raise HTTPException(status_code=404, detail="Repair case not found")
            case = rows[0]
            item_rows = cursor.execute(
                """
                SELECT r.ItemCode, ci.ItemLabel, r.Outcome, d.Severity, d.Finding
                FROM dbo.ChecklistResult AS r
                JOIN dbo.ChecklistItem AS ci
                    ON ci.ChecklistVersionId = r.ChecklistVersionId AND ci.ItemCode = r.ItemCode
                LEFT JOIN dbo.Defect AS d
                    ON d.InspectionId = r.InspectionId AND d.ItemCode = r.ItemCode
                WHERE r.InspectionId = ?
                ORDER BY CASE WHEN r.Outcome = 'FAIL' THEN 0 ELSE 1 END, r.ItemCode, d.DefectId;
                """,
                case.InitialInspectionId,
            ).fetchall() if case.InitialInspectionId is not None else []
            reinspection_item_rows = cursor.execute(
                """
                SELECT result.InspectionId, result.ItemCode, item.ItemLabel, result.Outcome,
                       defect.Severity, defect.Finding
                FROM dbo.ReinspectionAttempt AS attempt
                JOIN dbo.ChecklistResult AS result ON result.InspectionId = attempt.InspectionId
                JOIN dbo.ChecklistItem AS item
                  ON item.ChecklistVersionId = result.ChecklistVersionId AND item.ItemCode = result.ItemCode
                LEFT JOIN dbo.Defect AS defect
                  ON defect.InspectionId = result.InspectionId AND defect.ItemCode = result.ItemCode
                WHERE attempt.RepairCaseId = ?
                ORDER BY attempt.AttemptNumber,
                         CASE WHEN result.Outcome = 'FAIL' THEN 0 ELSE 1 END,
                         result.ItemCode, defect.DefectId;
                """,
                repair_case_id,
            ).fetchall()
            repair_rows = cursor.execute(
                """
                SELECT RepairAttemptId, PerformedAt, Technician, WorkDescription, Outcome
                FROM dbo.RepairAttempt WHERE RepairCaseId = ?
                ORDER BY PerformedAt, RepairAttemptId;
                """,
                repair_case_id,
            ).fetchall()
            repair_items_by_id = {}
            if cursor.execute("SELECT OBJECT_ID(N'dbo.RepairAttemptWorkItem', N'U');").fetchval() is not None:
                work_item_rows = cursor.execute(
                    """
                    SELECT item.RepairAttemptId, item.RepairAttemptWorkItemId, item.WorkType,
                           item.ApprovedScope, item.Description, item.PartCode, item.Quantity, item.LaborMinutes
                    FROM dbo.RepairAttemptWorkItem AS item
                    JOIN dbo.RepairAttempt AS repair ON repair.RepairAttemptId = item.RepairAttemptId
                    WHERE repair.RepairCaseId = ?
                    ORDER BY item.RepairAttemptId, item.RepairAttemptWorkItemId;
                    """,
                    repair_case_id,
                ).fetchall()
                for work_item in work_item_rows:
                    repair_items_by_id.setdefault(work_item.RepairAttemptId, []).append(
                        RepairAttemptWorkItem(
                            work_item_id=work_item.RepairAttemptWorkItemId, work_type=work_item.WorkType,
                            approved_scope=work_item.ApprovedScope, description=work_item.Description,
                            part_code=work_item.PartCode,
                            quantity=float(work_item.Quantity) if work_item.Quantity is not None else None,
                            labor_minutes=work_item.LaborMinutes,
                        )
                    )
            approval = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
            diagnostic_rows = None
            if cursor.execute("SELECT OBJECT_ID(N'dbo.DiagnosticCheck', N'U');").fetchval() is not None:
                diagnostic_rows = cursor.execute(
                    """
                    SELECT DiagnosticCheckId, CheckPerformed, Observation, Technician, RecordedAt
                    FROM dbo.DiagnosticCheck WHERE RepairCaseId = ?
                    ORDER BY RecordedAt, DiagnosticCheckId;
                    """,
                    repair_case_id,
                ).fetchall()
            follow_up_rows = cursor.execute(
                """
                SELECT FollowUpContactId, RepairApprovalId, ContactedAt, ContactMethod,
                       ContactResult, RecordedBy, Note, NextFollowUpAt
                FROM dbo.AdvisorFollowUpContact
                WHERE RepairCaseId = ?
                ORDER BY ContactedAt, FollowUpContactId;
                """,
                repair_case_id,
            ).fetchall()
            last_attempt = rows[-1]
            last_inspection_id = last_attempt.InspectionId or case.InitialInspectionId
            last_outcome = last_attempt.Outcome if last_attempt.InspectionId else case.InitialOutcome
            latest_repair = latest_repair_for_approval(cursor, repair_case_id, approval.RepairApprovalId) if (
                approval and approval.ApprovalStatus == "APPROVED"
            ) else None
            failed_at = failed_verification_time(
                last_outcome, last_attempt.InspectedAt, last_attempt.VerifiedRepairId,
                latest_repair.RepairAttemptId if latest_repair else None,
                latest_repair.RecordedAt if latest_repair else None,
            ) if last_attempt.InspectionId is not None else None
            handoff_enabled = bool(failed_at and workshop_availability_available(cursor))
            assignment = latest_workshop_assignment(cursor, repair_case_id) if workshop_tracking_available(cursor) else None
            reassigned = assignment and assignment.Assignee and assignment.InspectionId == last_attempt.InspectionId
            progress = case_progress(
                case, approval.ApprovalStatus if approval else None,
                approval.RequestedAt if approval else None, approval.DecidedAt if approval else None,
                latest_repair.RecordedAt if latest_repair else None, failed_at,
                awaiting_reassignment=handoff_enabled and not reassigned,
                reassigned_at=assignment.RecordedAt if reassigned else None,
            )
            advisory_rows = cursor.execute(
                """
                SELECT ci.ItemLabel, d.Severity, d.Finding
                FROM dbo.Defect AS d
                JOIN dbo.Inspection AS i ON i.InspectionId = d.InspectionId
                JOIN dbo.ChecklistItem AS ci
                    ON ci.ChecklistVersionId = i.ChecklistVersionId AND ci.ItemCode = d.ItemCode
                WHERE d.InspectionId = ? AND ci.FailInspectionOnFailure = 0
                ORDER BY ci.ItemCode, d.DefectId;
                """,
                last_inspection_id,
            ).fetchall() if last_inspection_id is not None else []
            failed_items = cursor.execute(
                """
                SELECT result.ItemCode,
                       MAX(CASE WHEN result.InspectionId = ? THEN 1 ELSE 0 END) AS FailedLastInspection
                FROM dbo.ChecklistResult AS result
                WHERE result.Outcome = 'FAIL' AND (
                    result.InspectionId = ?
                    OR EXISTS (
                        SELECT 1 FROM dbo.ReinspectionAttempt AS prior
                        WHERE prior.RepairCaseId = ? AND prior.InspectionId = result.InspectionId
                    )
                )
                GROUP BY result.ItemCode
                ORDER BY result.ItemCode;
                """,
                last_inspection_id, case.InitialInspectionId, repair_case_id,
            ).fetchall() if last_inspection_id is not None else []

    items = {}
    for row in item_rows:
        if row.ItemCode not in items:
            items[row.ItemCode] = InitialInspectionItem(
                item_code=row.ItemCode, label=row.ItemLabel,
                system=checklist_system(row.ItemCode, row.ItemLabel), outcome=row.Outcome,
            )
        if row.Finding is not None:
            items[row.ItemCode].findings.append(
                InspectionFinding(severity=row.Severity, finding=row.Finding)
            )

    reinspection_items = {}
    for row in reinspection_item_rows:
        inspection_items = reinspection_items.setdefault(row.InspectionId, {})
        if row.ItemCode not in inspection_items:
            inspection_items[row.ItemCode] = InitialInspectionItem(
                item_code=row.ItemCode, label=row.ItemLabel,
                system=checklist_system(row.ItemCode, row.ItemLabel), outcome=row.Outcome,
            )
        if row.Finding is not None:
            inspection_items[row.ItemCode].findings.append(
                InspectionFinding(severity=row.Severity, finding=row.Finding)
            )

    return RepairCaseHistory(
        closeout_reason=case.CloseoutReason,
        closed_by=case.ClosedBy,
        cancellation_reason=case.CancellationReason,
        cancelled_by=case.CancelledBy,
        current_repair_outcome=latest_repair.Outcome if latest_repair else None,
        retest_without_new_repair=progress.stage in ("REINSPECTION_FAILED", "AWAITING_REASSIGNMENT"),
        pending_qc_inspection_id=last_attempt.InspectionId if progress.stage == "AWAITING_REASSIGNMENT" else None,
        advisories=[AdvisoryFinding(label=row.ItemLabel, severity=row.Severity, finding=row.Finding)
                    for row in advisory_rows],
        repair_case_id=case.RepairCaseId,
        vehicle_id=case.VehicleId,
        fleet_code=case.FleetCode,
        vin=case.VIN,
        vehicle_make=case.VehicleMake,
        vehicle_model=case.VehicleModel,
        vehicle_model_year=case.VehicleModelYear,
        vehicle_powertrain=case.VehiclePowertrain,
        initial_inspection_id=case.InitialInspectionId,
        status="CLOSED" if case.ClosedAt is not None else "OPEN",
        is_test_data=bool(case.IsTestData),
        opened_at=case.OpenedAt,
        closed_at=case.ClosedAt,
        closing_inspection_id=case.ClosingInspectionId,
        customer_concern=case.CustomerConcern,
        customer_name=case.CustomerName,
        customer_phone=case.CustomerPhone,
        customer_email=case.CustomerEmail,
        odometer_km=case.OdometerKm,
        odometer_miles=case.OdometerMiles,
        initial_inspection=InitialInspectionDetails(
            inspected_at=case.InitialInspectedAt.replace(tzinfo=timezone.utc),
            outcome=case.InitialOutcome,
            checklist_title=case.ChecklistTitle,
            checklist_version=case.ChecklistVersion,
            items=list(items.values()),
            unanswered_count=max(0, case.TotalItems - len(items)),
        ) if case.InitialInspectionId is not None else None,
        can_reinspect=progress.stage in ("READY_FOR_REINSPECTION", "REINSPECTION_FAILED"),
        progress=progress,
        handoff=case_handoff(
            progress, "CLOSED" if case.ClosedAt is not None else "OPEN",
            assignment.Assignee if assignment else None,
            assignment.StaffRole if assignment else None,
        ),
        previous_failed_item_codes=[row.ItemCode for row in failed_items if row.FailedLastInspection],
        required_verification_item_codes=[row.ItemCode for row in failed_items],
        attempts=[
            ReinspectionHistoryItem(
                verified_approval_id=row.VerifiedApprovalId,
                verified_repair_id=row.VerifiedRepairId,
                attempt_number=row.AttemptNumber,
                inspection_id=row.InspectionId,
                outcome=row.Outcome,
                inspected_at=row.InspectedAt,
                repair_note=row.RepairNote,
                unanswered_count=max(0, row.TotalItems - row.AnsweredItems),
                items=list(reinspection_items.get(row.InspectionId, {}).values()),
            )
            for row in rows
            if row.AttemptNumber is not None
        ],
        diagnostic_checks=(
            [diagnostic_check_from_row(row) for row in diagnostic_rows]
            if diagnostic_rows is not None else None
        ),
        repair_attempts=[
            RepairAttemptHistoryItem(
                repair_attempt_id=row.RepairAttemptId,
                performed_at=row.PerformedAt.replace(tzinfo=timezone.utc),
                technician=row.Technician,
                work_description=row.WorkDescription,
                outcome=row.Outcome,
                work_items=repair_items_by_id.get(row.RepairAttemptId, []),
            )
            for row in repair_rows
        ],
        follow_up_contacts=[advisor_follow_up_contact_from_row(row) for row in follow_up_rows],
    )


@app.patch("/repair-cases/{repair_case_id}/intake")
def update_case_intake(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)], intake: CaseIntake,
) -> CaseIntake:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        require_open_repair_case(lock_repair_case(cursor, repair_case_id))
        cursor.execute(
            """
            UPDATE dbo.RepairCase
            SET CustomerConcern = ?, CustomerName = ?, CustomerPhone = ?, CustomerEmail = ?,
                OdometerKm = ?, OdometerMiles = ?
            WHERE RepairCaseId = ?;
            """,
            intake.customer_concern, intake.customer_name, intake.customer_phone, intake.customer_email,
            intake.odometer_km, intake.odometer_miles, repair_case_id,
        )
        db.commit()
    return intake


class SetTestDataFlag(BaseModel):
    is_test_data: bool
    recorded_by: ShortApprovalText


@app.put("/repair-cases/{repair_case_id}/test-data")
def set_case_test_data_flag(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    change: SetTestDataFlag,
):
    actor = current_staff.get()
    if actor is not None and actor.role != "supervisor":
        raise HTTPException(status_code=403, detail="Only a supervisor can classify a case as test data.")
    change = audited(change, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        row = cursor.execute(
            """
            UPDATE dbo.RepairCase
            SET IsTestData = ?, TestDataFlagChangedAt = SYSUTCDATETIME(), TestDataFlagChangedBy = ?
            OUTPUT inserted.IsTestData, inserted.TestDataFlagChangedAt, inserted.TestDataFlagChangedBy
            WHERE RepairCaseId = ?;
            """,
            change.is_test_data, change.recorded_by, repair_case_id,
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Repair case not found")
        db.commit()
    return {
        "repair_case_id": repair_case_id,
        "is_test_data": bool(row.IsTestData),
        "changed_at": row.TestDataFlagChangedAt.replace(tzinfo=timezone.utc),
        "changed_by": row.TestDataFlagChangedBy,
    }


class PreviousVisit(CaseIntake):
    repair_case_id: int
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    status: Literal["OPEN", "CLOSED"]
    final_outcome: Literal["PASS", "FAIL", "CLOSED_WITHOUT_REPAIR", "PASS_WITH_ADVISORIES", "CANCELLED", "REPAIR_VERIFIED"] | None
    last_repair: str | None


@app.get("/repair-cases/{repair_case_id}/previous-visits")
def list_previous_visits(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
) -> list[PreviousVisit]:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        current = cursor.execute(
            "SELECT VehicleId FROM dbo.RepairCase WHERE RepairCaseId = ?;",
            repair_case_id,
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Repair case not found")
        cancellation_reason = "prior.CancellationReason" if supports_case_cancellation(cursor) else "NULL"
        rows = cursor.execute(
            f"""
            SELECT prior.RepairCaseId, prior.OpenedAt, prior.ClosedAt, prior.CustomerConcern,
                   prior.CustomerName, prior.CustomerPhone, prior.CustomerEmail, prior.OdometerKm, prior.OdometerMiles,
                   CASE WHEN prior.ClosedAt IS NULL THEN 'OPEN' ELSE 'CLOSED' END AS CaseStatus,
                   CASE WHEN {cancellation_reason} IS NOT NULL THEN 'CANCELLED'
                        WHEN prior.CloseoutReason IS NOT NULL THEN 'CLOSED_WITHOUT_REPAIR'
                        WHEN prior.ClosingInspectionId IS NOT NULL AND outcome.Outcome = 'PASS'
                            AND EXISTS (
                                SELECT 1 FROM dbo.ChecklistItem AS ci
                                WHERE ci.ChecklistVersionId = outcome.ChecklistVersionId AND NOT EXISTS (
                                    SELECT 1 FROM dbo.ChecklistResult AS result
                                    WHERE result.InspectionId = outcome.InspectionId AND result.ItemCode = ci.ItemCode
                                )
                            ) THEN 'REPAIR_VERIFIED'
                        WHEN outcome.Outcome = 'PASS' AND EXISTS (
                            SELECT 1 FROM dbo.ChecklistResult AS result
                            WHERE result.InspectionId = outcome.InspectionId AND result.Outcome = 'FAIL'
                        ) THEN 'PASS_WITH_ADVISORIES'
                        ELSE outcome.Outcome END AS FinalOutcome,
                   repair.WorkDescription AS LastRepair
            FROM dbo.RepairCase AS prior
            LEFT JOIN dbo.Inspection AS outcome
                ON outcome.InspectionId = COALESCE(prior.ClosingInspectionId, prior.InitialInspectionId)
            OUTER APPLY (
                SELECT TOP (1) WorkDescription
                FROM dbo.RepairAttempt WHERE RepairCaseId = prior.RepairCaseId
                ORDER BY PerformedAt DESC, RepairAttemptId DESC
            ) AS repair
            WHERE prior.VehicleId = ? AND prior.RepairCaseId <> ?
            ORDER BY prior.OpenedAt DESC, prior.RepairCaseId DESC;
            """,
            current.VehicleId, repair_case_id,
        ).fetchall()
    return [
        PreviousVisit(
            repair_case_id=row.RepairCaseId, opened_at=row.OpenedAt.replace(tzinfo=timezone.utc),
            closed_at=row.ClosedAt.replace(tzinfo=timezone.utc) if row.ClosedAt is not None else None,
            status=row.CaseStatus,
            customer_concern=row.CustomerConcern, odometer_km=row.OdometerKm, odometer_miles=row.OdometerMiles,
            customer_name=row.CustomerName, customer_phone=row.CustomerPhone, customer_email=row.CustomerEmail,
            final_outcome=row.FinalOutcome, last_repair=row.LastRepair,
        )
        for row in rows
    ]


class ConcernLinkCreate(BaseModel):
    related_repair_case_id: int = Field(gt=0, le=2147483647)
    relationship: Literal["SAME_CONCERN", "NEW_CONCERN"]
    recorded_by: ShortApprovalText
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


def concern_link_from_row(row):
    return {
        "concern_link_id": row.ConcernLinkId,
        "repair_case_id": row.RepairCaseId,
        "related_repair_case_id": row.RelatedRepairCaseId,
        "relationship": row.Relationship,
        "recorded_by": row.RecordedBy,
        "recorded_at": row.RecordedAt.replace(tzinfo=timezone.utc),
        "note": row.Note,
    }


@app.get("/repair-cases/{repair_case_id}/concern-links")
def list_concern_links(repair_case_id: Annotated[int, Path(gt=0, le=2147483647)]):
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if cursor.execute("SELECT OBJECT_ID(N'dbo.RepairCaseConcernLink', N'U');").fetchval() is None:
            raise HTTPException(status_code=503, detail="Return-visit review requires migration 31.")
        rows = cursor.execute(
            """
            SELECT ConcernLinkId, RepairCaseId, RelatedRepairCaseId, Relationship, RecordedBy, RecordedAt, Note
            FROM dbo.RepairCaseConcernLink WHERE RepairCaseId = ?
            ORDER BY RecordedAt, ConcernLinkId;
            """,
            repair_case_id,
        ).fetchall()
    return [concern_link_from_row(row) for row in rows]


@app.post("/repair-cases/{repair_case_id}/concern-links", status_code=201)
def create_concern_link(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)], link: ConcernLinkCreate,
):
    actor = current_staff.get()
    if actor is not None and actor.role not in {"advisor", "supervisor"}:
        raise HTTPException(status_code=403, detail="Only a service advisor or supervisor can classify a return concern.")
    link = audited(link, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if cursor.execute("SELECT OBJECT_ID(N'dbo.RepairCaseConcernLink', N'U');").fetchval() is None:
            raise HTTPException(status_code=503, detail="Return-visit review requires migration 31.")
        current = cursor.execute(
            "SELECT VehicleId, ClosedAt FROM dbo.RepairCase WHERE RepairCaseId = ?;", repair_case_id,
        ).fetchone()
        related = cursor.execute(
            "SELECT VehicleId FROM dbo.RepairCase WHERE RepairCaseId = ?;", link.related_repair_case_id,
        ).fetchone()
        if current is None or related is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        if current.VehicleId != related.VehicleId:
            raise HTTPException(status_code=422, detail="Link only cases for the same vehicle.")
        if repair_case_id == link.related_repair_case_id:
            raise HTTPException(status_code=422, detail="A case cannot be linked to itself.")
        if current.ClosedAt is not None:
            raise HTTPException(status_code=409, detail="Return-concern decisions can only be recorded on an open case.")
        existing = cursor.execute(
            """
            SELECT ConcernLinkId, RepairCaseId, RelatedRepairCaseId, Relationship, RecordedBy, RecordedAt, Note
            FROM dbo.RepairCaseConcernLink WHERE RepairCaseId = ? AND RelatedRepairCaseId = ?;
            """,
            repair_case_id, link.related_repair_case_id,
        ).fetchone()
        if existing is not None:
            if existing.Relationship != link.relationship:
                raise HTTPException(status_code=409, detail="This prior case already has a different return-concern decision.")
            return concern_link_from_row(existing)
        row = cursor.execute(
            """
            INSERT dbo.RepairCaseConcernLink
                (RepairCaseId, RelatedRepairCaseId, Relationship, RecordedBy, Note)
            OUTPUT inserted.ConcernLinkId, inserted.RepairCaseId, inserted.RelatedRepairCaseId,
                   inserted.Relationship, inserted.RecordedBy, inserted.RecordedAt, inserted.Note
            VALUES (?, ?, ?, ?, ?);
            """,
            repair_case_id, link.related_repair_case_id, link.relationship, link.recorded_by, link.note,
        ).fetchone()
        db.commit()
    return concern_link_from_row(row)


TimingMetric = Literal["INTAKE_TO_INSPECTION", "REQUEST_TO_DECISION", "REPAIR_TO_REINSPECTION"]


class WorkflowTiming(BaseModel):
    metric: TimingMetric
    average_seconds: float
    sample_count: int



class WorkshopEventBase(BaseModel):
    recorded_by: ShortApprovalText


class WorkshopAssignment(WorkshopEventBase):
    kind: Literal["ASSIGNMENT"]
    assignee: ShortApprovalText | None = None
    staff_role: Literal["TECHNICIAN"] | None = None
    note: ApprovalScopeText | None = None
    expected_assignment_id: int | None = Field(default=None, gt=0, le=2147483647)
    expected_failed_inspection_id: int | None = Field(default=None, gt=0, le=2147483647)

    @model_validator(mode="after")
    def validate_assignment(self):
        if (self.assignee is None) != (self.staff_role is None):
            raise ValueError("Provide both an assignee and a staff role, or leave both empty.")
        return self


class WorkshopProposalLink(WorkshopEventBase):
    kind: Literal["PROPOSAL_LINK"]
    defect_id: int = Field(gt=0, le=2147483647)
    repair_approval_id: int = Field(gt=0, le=2147483647)
    note: ApprovalScopeText | None = None


class WorkshopFindingOutcome(WorkshopEventBase):
    kind: Literal["FINDING_OUTCOME"]
    defect_id: int = Field(gt=0, le=2147483647)
    repair_attempt_id: int = Field(gt=0, le=2147483647)
    finding_outcome: RepairOutcome
    note: ApprovalScopeText | None = None


class WorkshopAdvisoryReview(WorkshopEventBase):
    kind: Literal["ADVISORY_REVIEW"]
    inspection_id: int = Field(gt=0, le=2147483647)
    note: ApprovalScopeText


WorkshopEventCreate = Annotated[
    WorkshopAssignment | WorkshopProposalLink | WorkshopFindingOutcome | WorkshopAdvisoryReview,
    Field(discriminator="kind"),
]


def workshop_tracking_available(cursor) -> bool:
    return bool(cursor.execute("SELECT OBJECT_ID(N'dbo.WorkshopCaseEvent', N'U');").fetchval())



TECHNICIAN_BUSY_STAGES = {
    "AWAITING_INSPECTION", "NEEDS_PROPOSAL", "DECLINED", "WITHDRAWN",
    "READY_FOR_REPAIR", "REINSPECTION_FAILED",
}


def workshop_availability_available(cursor) -> bool:
    return bool(cursor.execute("SELECT OBJECT_ID(N'dbo.WorkshopTechnician', N'U');").fetchval())


def latest_workshop_assignment(cursor, repair_case_id):
    return cursor.execute(
        """
        SELECT TOP (1) WorkshopCaseEventId, Assignee, StaffRole, InspectionId, RecordedAt, Note
        FROM dbo.WorkshopCaseEvent WHERE RepairCaseId = ? AND EventKind = 'ASSIGNMENT'
        ORDER BY WorkshopCaseEventId DESC;
        """,
        repair_case_id,
    ).fetchone()


def technician_key(name):
    return name.strip().casefold()


def technician_workloads(rows):
    loads = {}
    for row in rows:
        if row.StaffRole == "TECHNICIAN" and row.Assignee and progress_from_search_row(row).stage in TECHNICIAN_BUSY_STAGES:
            loads.setdefault(technician_key(row.Assignee), []).append(row.RepairCaseId)
    return loads


def known_workshop_technicians(cursor):
    rows = cursor.execute(
        """
        SELECT TechnicianName FROM dbo.WorkshopTechnician
        UNION
        SELECT event.Assignee
        FROM dbo.WorkshopCaseEvent AS event
        JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = event.RepairCaseId
        JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
        WHERE event.EventKind = 'ASSIGNMENT' AND event.StaffRole = 'TECHNICIAN' AND event.Assignee IS NOT NULL
          AND v.FleetCode NOT LIKE 'PILOT-%' AND v.FleetCode NOT LIKE 'SMOKE-%'
          AND v.FleetCode NOT LIKE 'DEMO-%' AND v.FleetCode NOT LIKE 'TEST-%' AND v.FleetCode NOT LIKE 'QA-%';
        """
    ).fetchall()
    names = {technician_key(row.TechnicianName): row.TechnicianName for row in rows}
    names.update({technician_key(person.name): person.name for person in staff_access.accounts.values()
                  if person.role == "technician"})
    return names


def require_qc_handoff_complete(cursor, repair_case_id):
    if not workshop_availability_available(cursor):
        return
    rows = read_case_rows(cursor, "rc.RepairCaseId = ?", (repair_case_id,))
    if rows and progress_from_search_row(rows[0]).stage == "AWAITING_REASSIGNMENT":
        raise HTTPException(status_code=409, detail="Failed QC is awaiting supervisor reassignment.")


def lock_technician_capacity(cursor):
    lock_result = cursor.execute(
        """
        SET NOCOUNT ON;
        IF @@TRANCOUNT = 0 BEGIN TRANSACTION;
        DECLARE @result int;
        EXEC @result = sys.sp_getapplock @Resource = N'VehicleInspection.TechnicianAssignment',
            @LockMode = 'Exclusive', @LockOwner = 'Transaction', @LockTimeout = 5000;
        SELECT @result;
        """
    ).fetchval()
    if lock_result is None or lock_result < 0:
        raise HTTPException(status_code=409, detail="Another assignment is being saved. Refresh and try again.")


def technician_shifts(cursor):
    if not cursor.execute("SELECT OBJECT_ID(N'dbo.WorkshopTechnicianShift', N'U');").fetchval():
        return None
    return {row.NameKey: bool(row.OnShift) for row in cursor.execute(
        "SELECT NameKey, OnShift FROM dbo.WorkshopTechnicianShift;"
    ).fetchall()}


def release_unavailable_owner(cursor, repair_case_id, recorded_by):
    rows = read_case_rows(cursor, "rc.ClosedAt IS NULL")
    case = next(row for row in rows if row.RepairCaseId == repair_case_id)
    if case.StaffRole != "TECHNICIAN" or not case.Assignee:
        return
    key = technician_key(case.Assignee)
    other_jobs = technician_workloads(rows).get(key, ())
    if any(other != repair_case_id for other in other_jobs) or (technician_shifts(cursor) or {}).get(key) is False:
        cursor.execute(
            "INSERT dbo.WorkshopCaseEvent (RepairCaseId, EventKind, RecordedBy) VALUES (?, 'ASSIGNMENT', ?);",
            repair_case_id, recorded_by,
        )


def save_workshop_assignment(db, cursor, repair_case_id, event):
    if not workshop_availability_available(cursor):
        raise HTTPException(status_code=409, detail="Technician availability requires database migration 28.")
    lock_technician_capacity(cursor)
    require_open_repair_case(lock_repair_case(cursor, repair_case_id))
    rows = read_case_rows(cursor, "rc.ClosedAt IS NULL")
    current = next(row for row in rows if row.RepairCaseId == repair_case_id)
    stage = progress_from_search_row(current).stage
    pending_id = current.VerificationInspectionId if stage == "AWAITING_REASSIGNMENT" else None
    if (("expected_assignment_id" in event.model_fields_set and event.expected_assignment_id != current.AssignmentId)
            or ("expected_failed_inspection_id" in event.model_fields_set and event.expected_failed_inspection_id != pending_id)):
        raise HTTPException(status_code=409, detail="The assignment or QC handoff changed. Refresh the case and try again.")

    handoff_id = note = None
    assignee = event.assignee
    if pending_id is not None:
        if not assignee or not event.note:
            raise HTTPException(status_code=422, detail="Choose a technician and enter a handoff note for failed QC.")
        handoff_id, note = pending_id, event.note
    elif stage == "REINSPECTION_FAILED" and assignee:
        # Carry the specific QC handoff forward if a supervisor changes the technician again.
        handoff_id = current.HandoffInspectionId
        if handoff_id is not None:
            assignment = latest_workshop_assignment(cursor, repair_case_id)
            note = event.note or assignment.Note

    if assignee:
        key = technician_key(assignee)
        names = known_workshop_technicians(cursor)
        if key not in names:
            raise HTTPException(status_code=422, detail="Add this technician to the workshop before assigning a case.")
        if (technician_shifts(cursor) or {}).get(key) is False:
            raise HTTPException(status_code=409, detail="This technician is off shift.")
        assignee = names[key]
        busy = next((case_id for case_id in technician_workloads(rows).get(key, ()) if case_id != repair_case_id), None)
        if busy is not None:
            raise HTTPException(status_code=409, detail=f"{assignee} is working on case #{busy}. Choose an available technician.")
    cursor.execute(
        """
        INSERT dbo.WorkshopCaseEvent (RepairCaseId, EventKind, RecordedBy, Assignee, StaffRole, InspectionId, Note)
        VALUES (?, 'ASSIGNMENT', ?, ?, ?, ?, ?);
        """,
        repair_case_id, event.recorded_by, assignee, event.staff_role, handoff_id, note,
    )
    db.commit()


class RegisterWorkshopTechnician(WorkshopEventBase):
    name: ShortApprovalText


@app.post("/workshop/technicians", status_code=201)
def register_workshop_technician(technician: RegisterWorkshopTechnician):
    technician = audited(technician, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if not workshop_availability_available(cursor):
            raise HTTPException(status_code=409, detail="Technician availability requires database migration 28.")
        try:
            cursor.execute(
                "INSERT dbo.WorkshopTechnician (TechnicianName, AddedBy) VALUES (?, ?);",
                technician.name, technician.recorded_by,
            )
            db.commit()
        except pyodbc.IntegrityError as error:
            if is_duplicate_key(error):
                raise HTTPException(status_code=409, detail="This technician is already in the workshop directory.") from error
            raise
    return {"name": technician.name}


class TechnicianShift(RegisterWorkshopTechnician):
    on_shift: bool


class AssignmentAcknowledgementCreate(BaseModel):
    assignment_event_id: int = Field(gt=0, le=2147483647)
    response: Literal["ACCEPTED", "FLAGGED"]
    recorded_by: ShortApprovalText
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def require_flag_note(self):
        if self.response == "FLAGGED" and not self.note:
            raise ValueError("Add a short note when flagging an assignment.")
        return self


@app.post("/repair-cases/{repair_case_id}/assignment-acknowledgements", status_code=201)
def acknowledge_assignment(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    acknowledgement: AssignmentAcknowledgementCreate,
):
    actor = current_staff.get()
    if actor is not None and actor.role != "technician":
        raise HTTPException(status_code=403, detail="Only the assigned technician can acknowledge this assignment.")
    acknowledgement = audited(acknowledgement, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if cursor.execute("SELECT OBJECT_ID(N'dbo.WorkshopAssignmentAcknowledgement', N'U');").fetchval() is None:
            raise HTTPException(status_code=503, detail="Assignment acknowledgment requires migration 31.")
        case = cursor.execute(
            "SELECT ClosedAt FROM dbo.RepairCase WHERE RepairCaseId = ?;", repair_case_id,
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Repair case not found.")
        if case.ClosedAt is not None:
            raise HTTPException(status_code=409, detail="Closed cases cannot receive assignment acknowledgments.")
        owner = cursor.execute(
            """
            SELECT TOP (1) WorkshopCaseEventId, Assignee, StaffRole
            FROM dbo.WorkshopCaseEvent
            WHERE RepairCaseId = ? AND EventKind = 'ASSIGNMENT'
            ORDER BY WorkshopCaseEventId DESC;
            """,
            repair_case_id,
        ).fetchone()
        if (owner is None or owner.WorkshopCaseEventId != acknowledgement.assignment_event_id
                or owner.StaffRole != "TECHNICIAN" or not owner.Assignee
                or owner.Assignee.strip().casefold() != acknowledgement.recorded_by.strip().casefold()):
            raise HTTPException(status_code=409, detail="This assignment is no longer assigned to you.")
        if acknowledgement.response == "ACCEPTED":
            shifts = technician_shifts(cursor)
            if shifts is not None and shifts.get(technician_key(owner.Assignee)) is False:
                raise HTTPException(status_code=409, detail="You are marked off shift and cannot accept new work.")
        latest = cursor.execute(
            """
            SELECT TOP (1) AcknowledgementId, Response, Note, RecordedAt
            FROM dbo.WorkshopAssignmentAcknowledgement
            WHERE AssignmentEventId = ? ORDER BY RecordedAt DESC, AcknowledgementId DESC;
            """,
            acknowledgement.assignment_event_id,
        ).fetchone()
        if latest is not None and (latest.Response, latest.Note) == (acknowledgement.response, acknowledgement.note):
            return {"response": latest.Response, "recorded_at": latest.RecordedAt.replace(tzinfo=timezone.utc), "note": latest.Note}
        row = cursor.execute(
            """
            INSERT dbo.WorkshopAssignmentAcknowledgement
                (RepairCaseId, AssignmentEventId, Technician, Response, Note)
            OUTPUT inserted.Response, inserted.RecordedAt, inserted.Note
            VALUES (?, ?, ?, ?, ?);
            """,
            repair_case_id, acknowledgement.assignment_event_id, owner.Assignee,
            acknowledgement.response, acknowledgement.note,
        ).fetchone()
        db.commit()
    return {"response": row.Response, "recorded_at": row.RecordedAt.replace(tzinfo=timezone.utc), "note": row.Note}


@app.put("/workshop/technician-shift")
def set_technician_shift(change: TechnicianShift):
    change = audited(change, "recorded_by")
    key = technician_key(change.name)
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        lock_technician_capacity(cursor)
        if technician_shifts(cursor) is None:
            raise HTTPException(status_code=409, detail="Shift controls require migration 29.")
        if key not in known_workshop_technicians(cursor):
            raise HTTPException(status_code=422, detail="Unknown technician.")
        cursor.execute(
            """
            UPDATE dbo.WorkshopTechnicianShift SET OnShift = ?, ChangedBy = ?, ChangedAt = SYSUTCDATETIME()
            WHERE NameKey = ?;
            IF @@ROWCOUNT = 0
                INSERT dbo.WorkshopTechnicianShift (NameKey, OnShift, ChangedBy) VALUES (?, ?, ?);
            """, change.on_shift, change.recorded_by, key, key, change.on_shift, change.recorded_by,
        )
        db.commit()
    return {"on_shift": change.on_shift}


@app.get("/workshop/availability")
def workshop_availability():
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if not workshop_availability_available(cursor):
            return {"available": False, "technicians": []}
        names = known_workshop_technicians(cursor)
        rows = read_case_rows(cursor, "rc.ClosedAt IS NULL")
        loads = technician_workloads(rows)
        shifts = technician_shifts(cursor)
    return {"available": True, "shifts_enabled": shifts is not None, "technicians": [
        {"name": name, "case_ids": loads.get(key, []), "on_shift": (shifts or {}).get(key, True)}
        for key, name in sorted(names.items())
    ]}


@app.get("/repair-cases/{repair_case_id}/workshop")
def workshop_case_details(repair_case_id: Annotated[int, Path(gt=0, le=2147483647)]):
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if not cursor.execute("SELECT 1 FROM dbo.RepairCase WHERE RepairCaseId = ?;", repair_case_id).fetchval():
            raise HTTPException(status_code=404, detail="Repair case not found")
        findings = cursor.execute(
            """
            SELECT d.DefectId, d.InspectionId, d.ItemCode, ci.ItemLabel, d.Severity,
                   d.Finding, ci.FailInspectionOnFailure
            FROM dbo.Defect AS d
            JOIN dbo.Inspection AS i ON i.InspectionId = d.InspectionId
            JOIN dbo.ChecklistItem AS ci
              ON ci.ChecklistVersionId = i.ChecklistVersionId AND ci.ItemCode = d.ItemCode
            WHERE d.InspectionId IN (
                SELECT InitialInspectionId FROM dbo.RepairCase WHERE RepairCaseId = ?
                UNION ALL
                SELECT InspectionId FROM dbo.ReinspectionAttempt WHERE RepairCaseId = ?
            )
            ORDER BY i.InspectedAt, i.InspectionId, d.DefectId;
            """,
            repair_case_id, repair_case_id,
        ).fetchall()
        available = workshop_tracking_available(cursor)
        events = cursor.execute(
            """
            SELECT WorkshopCaseEventId, EventKind, RecordedBy, RecordedAt, Assignee, StaffRole, DefectId, RepairApprovalId,
                   RepairAttemptId, InspectionId, FindingOutcome, Note
            FROM dbo.WorkshopCaseEvent WHERE RepairCaseId = ? ORDER BY WorkshopCaseEventId;
            """,
            repair_case_id,
        ).fetchall() if available else []
        acknowledgement_available = cursor.execute(
            "SELECT OBJECT_ID(N'dbo.WorkshopAssignmentAcknowledgement', N'U');"
        ).fetchval() is not None
        acknowledgements = cursor.execute(
            """
            SELECT AcknowledgementId, AssignmentEventId, Technician, Response, RecordedAt, Note
            FROM dbo.WorkshopAssignmentAcknowledgement WHERE RepairCaseId = ?
            ORDER BY RecordedAt, AcknowledgementId;
            """,
            repair_case_id,
        ).fetchall() if acknowledgement_available else []
    return {
        "available": available,
        "findings": [
            {"defect_id": f.DefectId, "inspection_id": f.InspectionId, "item_code": f.ItemCode, "label": f.ItemLabel,
             "severity": f.Severity, "finding": f.Finding, "blocking": f.FailInspectionOnFailure}
            for f in findings
        ],
        "events": [
            {"event_id": e.WorkshopCaseEventId, "kind": e.EventKind, "recorded_by": e.RecordedBy, "recorded_at": e.RecordedAt.replace(tzinfo=timezone.utc),
             "assignee": e.Assignee, "staff_role": e.StaffRole, "defect_id": e.DefectId,
             "repair_approval_id": e.RepairApprovalId, "repair_attempt_id": e.RepairAttemptId,
             "inspection_id": e.InspectionId, "finding_outcome": e.FindingOutcome, "note": e.Note}
            for e in events
        ],
        "acknowledgements": [
            {"acknowledgement_id": a.AcknowledgementId, "assignment_event_id": a.AssignmentEventId,
             "technician": a.Technician, "response": a.Response,
             "recorded_at": a.RecordedAt.replace(tzinfo=timezone.utc), "note": a.Note}
            for a in acknowledgements
        ],
        "acknowledgements_available": acknowledgement_available,
    }


class RepairCaseAttachmentCreate(BaseModel):
    attachment_scope: Literal["CASE", "INSPECTION", "DEFECT", "REPAIR"]
    inspection_id: int | None = Field(default=None, gt=0, le=2147483647)
    defect_id: int | None = Field(default=None, gt=0, le=2147483647)
    repair_attempt_id: int | None = Field(default=None, gt=0, le=2147483647)
    original_file_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)]
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    content_base64: Annotated[str, StringConstraints(min_length=4, max_length=5592408)]
    caption: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    uploaded_by: ShortApprovalText

    @model_validator(mode="after")
    def validate_scope(self):
        valid = (
            self.attachment_scope == "CASE" and self.inspection_id is None and self.defect_id is None and self.repair_attempt_id is None
            or self.attachment_scope == "INSPECTION" and self.inspection_id is not None and self.defect_id is None and self.repair_attempt_id is None
            or self.attachment_scope == "DEFECT" and self.inspection_id is not None and self.defect_id is not None and self.repair_attempt_id is None
            or self.attachment_scope == "REPAIR" and self.inspection_id is None and self.defect_id is None and self.repair_attempt_id is not None
        )
        if not valid:
            raise ValueError("Choose one valid evidence attachment scope.")
        return self


def repair_case_attachment_metadata(row):
    return {
        "attachment_id": row.AttachmentId,
        "repair_case_id": row.RepairCaseId,
        "attachment_scope": row.AttachmentScope,
        "inspection_id": row.InspectionId,
        "defect_id": row.DefectId,
        "repair_attempt_id": row.RepairAttemptId,
        "original_file_name": row.OriginalFileName,
        "content_type": row.ContentType,
        "file_size": row.FileSize,
        "sha256": row.Sha256.hex() if isinstance(row.Sha256, bytes) else str(row.Sha256),
        "caption": row.Caption,
        "uploaded_by": row.UploadedBy,
        "uploaded_at": row.UploadedAt.replace(tzinfo=timezone.utc),
    }


def require_attachment_store(cursor):
    if cursor.execute("SELECT OBJECT_ID(N'dbo.RepairCaseAttachment', N'U');").fetchval() is None:
        raise HTTPException(status_code=503, detail="Internal evidence storage requires migration 31.")


@app.get("/repair-cases/{repair_case_id}/attachments")
def list_repair_case_attachments(repair_case_id: Annotated[int, Path(gt=0, le=2147483647)]):
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        require_attachment_store(cursor)
        rows = cursor.execute(
            """
            SELECT AttachmentId, RepairCaseId, AttachmentScope, InspectionId, DefectId, RepairAttemptId,
                   OriginalFileName, ContentType, FileSize, Sha256, Caption, UploadedBy, UploadedAt
            FROM dbo.RepairCaseAttachment WHERE RepairCaseId = ? ORDER BY UploadedAt, AttachmentId;
            """,
            repair_case_id,
        ).fetchall()
    return [repair_case_attachment_metadata(row) for row in rows]


@app.post("/repair-cases/{repair_case_id}/attachments", status_code=201)
def add_repair_case_attachment(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)], attachment: RepairCaseAttachmentCreate,
):
    actor = current_staff.get()
    if actor is not None and actor.role not in {"advisor", "supervisor", "technician", "quality-control"}:
        raise HTTPException(status_code=403, detail="This workspace cannot add case evidence.")
    attachment = audited(attachment, "uploaded_by")
    try:
        content = base64.b64decode(attachment.content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=422, detail="The image upload is not valid base64 data.") from error
    if not content or len(content) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Evidence images must be between 1 byte and 4 MB.")
    signatures = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    if not signatures[attachment.content_type]:
        raise HTTPException(status_code=422, detail="Image content does not match its declared JPEG, PNG, or WebP type.")
    safe_name = FilePath(attachment.original_file_name.replace("\\", "/")).name
    safe_name = re.sub(r"[^A-Za-z0-9._() -]", "_", safe_name).strip(" .")[:180] or "evidence-image"
    checksum = hashlib.sha256(content).digest()
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        require_attachment_store(cursor)
        if not cursor.execute("SELECT 1 FROM dbo.RepairCase WHERE RepairCaseId = ?;", repair_case_id).fetchval():
            raise HTTPException(status_code=404, detail="Repair case not found.")
        if actor is not None and actor.role == "technician":
            require_technician_assignment(cursor, repair_case_id)
        if attachment.attachment_scope == "INSPECTION" and not cursor.execute(
            """
            SELECT 1 FROM dbo.Inspection AS inspection
            WHERE inspection.InspectionId = ? AND (
                EXISTS (SELECT 1 FROM dbo.RepairCase WHERE RepairCaseId = ? AND InitialInspectionId = inspection.InspectionId)
                OR EXISTS (SELECT 1 FROM dbo.ReinspectionAttempt WHERE RepairCaseId = ? AND InspectionId = inspection.InspectionId)
            );
            """,
            attachment.inspection_id, repair_case_id, repair_case_id,
        ).fetchval():
            raise HTTPException(status_code=422, detail="Choose an inspection belonging to this case.")
        if attachment.attachment_scope == "DEFECT" and not cursor.execute(
            """
            SELECT 1 FROM dbo.Defect AS defect
            WHERE defect.DefectId = ? AND defect.InspectionId = ? AND (
                EXISTS (SELECT 1 FROM dbo.RepairCase WHERE RepairCaseId = ? AND InitialInspectionId = defect.InspectionId)
                OR EXISTS (SELECT 1 FROM dbo.ReinspectionAttempt WHERE RepairCaseId = ? AND InspectionId = defect.InspectionId)
            );
            """,
            attachment.defect_id, attachment.inspection_id, repair_case_id, repair_case_id,
        ).fetchval():
            raise HTTPException(status_code=422, detail="Choose a defect belonging to this case and inspection.")
        if attachment.attachment_scope == "REPAIR" and not cursor.execute(
            "SELECT 1 FROM dbo.RepairAttempt WHERE RepairAttemptId = ? AND RepairCaseId = ?;",
            attachment.repair_attempt_id, repair_case_id,
        ).fetchval():
            raise HTTPException(status_code=422, detail="Choose a repair attempt belonging to this case.")
        row = cursor.execute(
            """
            INSERT dbo.RepairCaseAttachment
                (RepairCaseId, AttachmentScope, InspectionId, DefectId, RepairAttemptId,
                 OriginalFileName, ContentType, FileSize, Sha256, Caption, FileBytes, UploadedBy)
            OUTPUT inserted.AttachmentId, inserted.RepairCaseId, inserted.AttachmentScope,
                   inserted.InspectionId, inserted.DefectId, inserted.RepairAttemptId,
                   inserted.OriginalFileName, inserted.ContentType, inserted.FileSize, inserted.Sha256,
                   inserted.Caption, inserted.UploadedBy, inserted.UploadedAt
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            repair_case_id, attachment.attachment_scope, attachment.inspection_id, attachment.defect_id,
            attachment.repair_attempt_id, safe_name, attachment.content_type, len(content), checksum,
            attachment.caption, pyodbc.Binary(content), attachment.uploaded_by,
        ).fetchone()
        saved = repair_case_attachment_metadata(row)
        db.commit()
    return saved


@app.get("/repair-case-attachments/{attachment_id}/file")
def get_repair_case_attachment_file(attachment_id: Annotated[int, Path(gt=0, le=2147483647)]):
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        require_attachment_store(cursor)
        row = cursor.execute(
            "SELECT OriginalFileName, ContentType, FileBytes FROM dbo.RepairCaseAttachment WHERE AttachmentId = ?;",
            attachment_id,
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Evidence image not found.")
    filename = re.sub(r"[^A-Za-z0-9._()-]", "_", row.OriginalFileName)[:180] or "evidence-image"
    return Response(
        content=bytes(row.FileBytes), media_type=row.ContentType,
        headers={"Content-Disposition": f'inline; filename="{filename}"',
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@app.post("/repair-cases/{repair_case_id}/workshop-events", status_code=201)
def record_workshop_event(repair_case_id: Annotated[int, Path(gt=0, le=2147483647)], event: WorkshopEventCreate):
    require_workshop_event_role(event.kind)
    event = audited(event, "recorded_by")
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        if not workshop_tracking_available(cursor):
            raise HTTPException(status_code=409, detail="Workshop tracking requires database migration 27.")
        if event.kind == "ASSIGNMENT":
            save_workshop_assignment(db, cursor, repair_case_id, event)
            return {"saved": True}
        case = lock_repair_case(cursor, repair_case_id)
        values = event.model_dump()
        if event.kind != "ADVISORY_REVIEW":
            require_qc_handoff_complete(cursor, repair_case_id)
        if event.kind != "ASSIGNMENT":
            defect_id = values.get("defect_id")
            inspection_id = values.get("inspection_id")
            finding = cursor.execute(
                """
                SELECT TOP (1) d.InspectionId, i.InspectedAt
                FROM dbo.Defect AS d
                JOIN dbo.Inspection AS i ON i.InspectionId = d.InspectionId
                JOIN dbo.ChecklistItem AS ci
                  ON ci.ChecklistVersionId = i.ChecklistVersionId AND ci.ItemCode = d.ItemCode
                WHERE ((d.DefectId = ?) OR (d.InspectionId = ? AND d.Severity = 'CRITICAL' AND ci.FailInspectionOnFailure = 0))
                  AND (d.InspectionId = ? OR EXISTS (
                      SELECT 1 FROM dbo.ReinspectionAttempt
                      WHERE InspectionId = d.InspectionId AND RepairCaseId = ?
                  ));
                """,
                defect_id, inspection_id, case.InitialInspectionId, repair_case_id,
            ).fetchone()
            if finding is None:
                raise HTTPException(status_code=422, detail="Choose a finding or critical advisory from this case.")
            if event.kind == "PROPOSAL_LINK":
                valid = cursor.execute(
                    """
                    SELECT 1 FROM dbo.RepairApproval
                    WHERE RepairCaseId = ? AND RepairApprovalId = ? AND RequestedAt >= ?;
                    """,
                    repair_case_id, event.repair_approval_id, finding.InspectedAt,
                ).fetchval()
            elif event.kind == "FINDING_OUTCOME":
                valid = cursor.execute(
                    """
                    SELECT 1 FROM dbo.RepairAttempt
                    WHERE RepairCaseId = ? AND RepairAttemptId = ? AND PerformedAt >= ?;
                    """,
                    repair_case_id, event.repair_attempt_id, finding.InspectedAt,
                ).fetchval()
            else:
                valid = True
            if not valid:
                raise HTTPException(status_code=422, detail="Choose work recorded for this case after the finding.")
        try:
            cursor.execute(
                """
                INSERT dbo.WorkshopCaseEvent (RepairCaseId, EventKind, RecordedBy, Assignee, StaffRole,
                    DefectId, RepairApprovalId, RepairAttemptId, InspectionId, FindingOutcome, Note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                repair_case_id, event.kind, event.recorded_by, values.get("assignee"), values.get("staff_role"),
                values.get("defect_id"), values.get("repair_approval_id"), values.get("repair_attempt_id"),
                values.get("inspection_id"), values.get("finding_outcome"), values.get("note"),
            )
            db.commit()
        except pyodbc.IntegrityError as error:
            if is_duplicate_key(error):
                raise HTTPException(status_code=409, detail="This workshop entry is already recorded.") from error
            raise
    return {"saved": True}


@app.get("/workshop/queue-details")
def workshop_queue_details(days: Annotated[int, Query(ge=1, le=365)] = 30):
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        available = workshop_tracking_available(cursor)
        assignment = """
            OUTER APPLY (
                SELECT TOP (1) Assignee, StaffRole FROM dbo.WorkshopCaseEvent
                WHERE RepairCaseId = rc.RepairCaseId AND EventKind = 'ASSIGNMENT'
                ORDER BY WorkshopCaseEventId DESC
            ) AS owner
        """ if available else "OUTER APPLY (SELECT NULL AS Assignee, NULL AS StaffRole) AS owner"
        review_filter = """
            AND NOT EXISTS (
                SELECT 1 FROM dbo.WorkshopCaseEvent AS review
                WHERE review.RepairCaseId = rc.RepairCaseId AND review.EventKind = 'ADVISORY_REVIEW'
                  AND review.InspectionId = d.InspectionId
            )
        """ if available else ""
        cancelled = "rc.CancellationReason" if supports_case_cancellation(cursor) else "NULL"
        rows = cursor.execute(
            f"""
            SELECT rc.RepairCaseId, owner.Assignee, owner.StaffRole, review.PendingReview,
                   CASE WHEN EXISTS (SELECT 1 FROM dbo.ReinspectionAttempt WHERE RepairCaseId = rc.RepairCaseId)
                        THEN 1 ELSE 0 END AS HasReinspection,
                   qc.FailedReinspectionCount,
                   CASE WHEN rc.ClosedAt < cutoff.Since OR rc.ClosedAt IS NULL THEN NULL
                        WHEN {cancelled} IS NOT NULL THEN 'CANCELLED'
                        WHEN rc.CloseoutReason IS NOT NULL THEN 'CLOSED_WITHOUT_REPAIR'
                        WHEN rc.ClosingInspectionId IS NOT NULL THEN 'REPAIR_VERIFIED'
                        WHEN initial.Outcome = 'PASS' THEN 'INITIAL_PASS'
                        ELSE 'OTHER' END AS Closure,
                   waits.DecisionCount, waits.WaitSeconds,
                   rc.IsTestData AS IsTestData
            FROM dbo.RepairCase AS rc
            JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
            LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
            CROSS JOIN (SELECT DATEADD(day, -?, SYSUTCDATETIME()) AS Since) AS cutoff
            {assignment}
            OUTER APPLY (
                SELECT COUNT(DISTINCT d.InspectionId) AS PendingReview
                FROM dbo.Defect AS d
                JOIN dbo.Inspection AS i ON i.InspectionId = d.InspectionId
                JOIN dbo.ChecklistItem AS ci
                  ON ci.ChecklistVersionId = i.ChecklistVersionId AND ci.ItemCode = d.ItemCode
                WHERE d.Severity = 'CRITICAL' AND ci.FailInspectionOnFailure = 0
                  AND (d.InspectionId = rc.InitialInspectionId OR EXISTS (
                      SELECT 1 FROM dbo.ReinspectionAttempt
                      WHERE InspectionId = d.InspectionId AND RepairCaseId = rc.RepairCaseId
                  ))
                  {review_filter}
            ) AS review
            OUTER APPLY (
                SELECT COUNT(*) AS FailedReinspectionCount
                FROM dbo.ReinspectionAttempt AS attempt
                JOIN dbo.Inspection AS inspection ON inspection.InspectionId = attempt.InspectionId
                WHERE attempt.RepairCaseId = rc.RepairCaseId AND inspection.Outcome = 'FAIL'
            ) AS qc
            OUTER APPLY (
                SELECT COUNT(*) AS DecisionCount, SUM(DATEDIFF_BIG(second, RequestedAt, DecidedAt)) AS WaitSeconds
                FROM dbo.RepairApproval
                WHERE RepairCaseId = rc.RepairCaseId AND ApprovalStatus IN ('APPROVED', 'DECLINED')
                  AND DecidedAt >= cutoff.Since
            ) AS waits;
            """,
            days,
        ).fetchall()
    return {
        "available": available,
        "cases": [
            {"case_id": r.RepairCaseId, "assignee": r.Assignee, "staff_role": r.StaffRole,
             "pending_review": r.PendingReview, "has_reinspection": bool(r.HasReinspection),
             "failed_reinspection_count": r.FailedReinspectionCount,
             "closure": r.Closure, "decision_count": r.DecisionCount,
             "wait_seconds": r.WaitSeconds or 0, "is_test_data": bool(r.IsTestData)}
            for r in rows
        ],
    }


class AdvisorFollowUp(BaseModel):
    repair_case_id: int
    fleet_code: str
    repair_approval_id: int
    requested_at: AwareDatetime
    diagnosis: str
    proposed_repair: str
    contact_count: int = 0
    last_contact_at: AwareDatetime | None = None
    last_contact_result: str | None = None
    last_contact_note: str | None = None
    next_follow_up_at: AwareDatetime | None = None
    follow_up_state: Literal["OVERDUE", "DUE", "UPCOMING", "UNSCHEDULED"] = "UNSCHEDULED"


@app.get("/service-advisor/follow-ups", response_model=list[AdvisorFollowUp])
def service_advisor_follow_ups() -> list[AdvisorFollowUp]:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        rows = cursor.execute(
            """
            SELECT rc.RepairCaseId, v.FleetCode, proposal.RepairApprovalId,
                   proposal.RequestedAt, proposal.Diagnosis, proposal.ProposedRepair,
                   contacts.ContactCount, latest.ContactedAt AS LastContactAt,
                   latest.ContactResult AS LastContactResult, latest.Note AS LastContactNote,
                   latest.NextFollowUpAt
            FROM dbo.RepairCase AS rc
            JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
            OUTER APPLY (
                SELECT TOP (1) RepairApprovalId, ApprovalStatus, RequestedAt, Diagnosis, ProposedRepair
                FROM dbo.RepairApproval
                WHERE RepairCaseId = rc.RepairCaseId
                ORDER BY RequestedAt DESC, RepairApprovalId DESC
            ) AS proposal
            OUTER APPLY (
                SELECT COUNT(*) AS ContactCount
                FROM dbo.AdvisorFollowUpContact
                WHERE RepairCaseId = rc.RepairCaseId AND RepairApprovalId = proposal.RepairApprovalId
            ) AS contacts
            OUTER APPLY (
                SELECT TOP (1) ContactedAt, ContactResult, Note, NextFollowUpAt
                FROM dbo.AdvisorFollowUpContact
                WHERE RepairCaseId = rc.RepairCaseId AND RepairApprovalId = proposal.RepairApprovalId
                ORDER BY ContactedAt DESC, FollowUpContactId DESC
            ) AS latest
            WHERE rc.ClosedAt IS NULL AND proposal.ApprovalStatus = 'PENDING'
              AND rc.IsTestData = 0
            ORDER BY CASE WHEN latest.NextFollowUpAt <= SYSUTCDATETIME() THEN 0
                          WHEN latest.NextFollowUpAt IS NULL THEN 2 ELSE 1 END,
                     latest.NextFollowUpAt ASC, proposal.RequestedAt ASC, rc.RepairCaseId ASC;
            """
        ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        AdvisorFollowUp(
            repair_case_id=row.RepairCaseId,
            fleet_code=row.FleetCode,
            repair_approval_id=row.RepairApprovalId,
            requested_at=row.RequestedAt.replace(tzinfo=timezone.utc),
            diagnosis=row.Diagnosis,
            proposed_repair=row.ProposedRepair,
            contact_count=row.ContactCount,
            last_contact_at=row.LastContactAt.replace(tzinfo=timezone.utc) if row.LastContactAt else None,
            last_contact_result=row.LastContactResult,
            last_contact_note=row.LastContactNote,
            next_follow_up_at=row.NextFollowUpAt.replace(tzinfo=timezone.utc) if row.NextFollowUpAt else None,
            follow_up_state=("UNSCHEDULED" if row.NextFollowUpAt is None else
                             "OVERDUE" if row.NextFollowUpAt.replace(tzinfo=timezone.utc) < now else
                             "DUE" if row.NextFollowUpAt.replace(tzinfo=timezone.utc) <= now else "UPCOMING"),
        )
        for row in rows
    ]


@app.post("/repair-cases/{repair_case_id}/approvals/{approval_id}/follow-ups", status_code=201)
def record_advisor_follow_up(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    approval_id: Annotated[int, Path(gt=0, le=2147483647)],
    contact: RecordAdvisorFollowUp,
) -> AdvisorFollowUpContact:
    actor = current_staff.get()
    if actor is not None and actor.role not in {"advisor", "supervisor"}:
        raise HTTPException(status_code=403, detail="Only a service advisor or supervisor can record customer follow-up.")
    contact = audited(contact, "recorded_by")
    next_follow_up = contact.next_follow_up_at.astimezone(timezone.utc).replace(tzinfo=None) if contact.next_follow_up_at else None
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        case = lock_repair_case(cursor, repair_case_id)
        require_open_repair_case(case)
        proposal = read_repair_approvals(cursor, repair_case_id, latest_only=True).fetchone()
        if proposal is None or proposal.RepairApprovalId != approval_id or proposal.ApprovalStatus != "PENDING":
            raise HTTPException(status_code=409, detail="Follow-up can only be recorded for the current pending proposal.")
        row = cursor.execute(
            """
            INSERT dbo.AdvisorFollowUpContact
                (RepairCaseId, RepairApprovalId, ContactMethod, ContactResult, RecordedBy, Note, NextFollowUpAt)
            OUTPUT inserted.FollowUpContactId, inserted.RepairApprovalId, inserted.ContactedAt,
                   inserted.ContactMethod, inserted.ContactResult, inserted.RecordedBy,
                   inserted.Note, inserted.NextFollowUpAt
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            repair_case_id, approval_id, contact.contact_method, contact.contact_result,
            contact.recorded_by, contact.note, next_follow_up,
        ).fetchone()
        saved = advisor_follow_up_contact_from_row(row)
        db.commit()
    return saved


@app.get("/repair-case-timings")
def get_repair_case_timings(days: Annotated[int, Query(ge=1, le=365)] = 30) -> list[WorkflowTiming]:
    with closing(connect_to_database()) as db, closing(db.cursor()) as cursor:
        rows = cursor.execute(
            f"""
            WITH Cutoff AS (
                SELECT DATEADD(day, -?, SYSUTCDATETIME()) AS Since
            ),
            Reinspections AS (
                SELECT ra.RepairCaseId, ra.VerifiedRepairId, i.InspectedAt,
                       LAG(i.InspectedAt) OVER (
                           PARTITION BY ra.RepairCaseId ORDER BY ra.AttemptNumber
                       ) AS PreviousInspectionAt
                FROM dbo.ReinspectionAttempt AS ra
                JOIN dbo.Inspection AS i ON i.InspectionId = ra.InspectionId
            ),
            Intervals AS (
                SELECT 'INTAKE_TO_INSPECTION' AS Metric, rc.RepairCaseId, i.InspectedAt AS OccurredAt,
                       DATEDIFF_BIG(second, rc.OpenedAt, i.InspectedAt) AS ElapsedSeconds
                FROM dbo.RepairCase AS rc
                JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
                JOIN dbo.Inspection AS i ON i.InspectionId = rc.InitialInspectionId
                LEFT JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = i.ChecklistVersionId
                CROSS JOIN Cutoff AS cutoff
                WHERE i.InspectedAt >= rc.OpenedAt AND i.InspectedAt >= cutoff.Since
                  AND rc.IsTestData = 0
                UNION ALL
                SELECT 'REQUEST_TO_DECISION', rc.RepairCaseId, approval.DecidedAt,
                       DATEDIFF_BIG(second, approval.RequestedAt, approval.DecidedAt)
                FROM dbo.RepairApproval AS approval
                JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = approval.RepairCaseId
                JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
                LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
                LEFT JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = initial.ChecklistVersionId
                CROSS JOIN Cutoff AS cutoff
                WHERE approval.ApprovalStatus IN ('APPROVED', 'DECLINED')
                  AND approval.DecidedAt >= cutoff.Since AND rc.IsTestData = 0
                UNION ALL
                SELECT 'REPAIR_TO_REINSPECTION', rc.RepairCaseId, r.InspectedAt,
                       DATEDIFF_BIG(second, repair.PerformedAt, r.InspectedAt)
                FROM Reinspections AS r
                JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = r.RepairCaseId
                JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
                LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
                LEFT JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = initial.ChecklistVersionId
                CROSS JOIN Cutoff AS cutoff
                CROSS APPLY (
                    SELECT TOP (1) PerformedAt
                    FROM dbo.RepairAttempt
                    WHERE RepairCaseId = r.RepairCaseId AND PerformedAt <= r.InspectedAt
                      AND RecordedAt <= r.InspectedAt
                      AND (r.VerifiedRepairId IS NULL OR RepairAttemptId = r.VerifiedRepairId)
                      AND (r.PreviousInspectionAt IS NULL OR PerformedAt > r.PreviousInspectionAt)
                    ORDER BY PerformedAt DESC, RepairAttemptId DESC
                ) AS repair
                WHERE r.InspectedAt >= cutoff.Since AND rc.IsTestData = 0
            )
            SELECT Metric, AVG(CAST(ElapsedSeconds AS float)) AS AverageSeconds, COUNT(*) AS SampleCount
            FROM Intervals GROUP BY Metric;
            """,
            days,
        ).fetchall()
    return [
        WorkflowTiming(metric=row.Metric, average_seconds=row.AverageSeconds, sample_count=row.SampleCount)
        for row in rows
    ]


class SaveResearchSource(BaseModel):
    source_url: HttpUrl
    verification_note: str = Field(min_length=1, max_length=1000)


class ResearchSource(BaseModel):
    research_source_id: int
    source_url: str
    verification_note: str


ARABIC_CHECKLIST_LABELS = {
    "BRAKE_LIGHT": "أضواء الفرامل",
    "HEADLIGHT": "المصابيح الأمامية",
}


class RepairResearch(BaseModel):
    suggested_query: str
    suggested_query_ar: str
    findings: list[str]
    sources: list[ResearchSource]


@app.get("/repair-cases/{repair_case_id}/research")
def get_repair_research(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
) -> RepairResearch:
    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            rows = cursor.execute(
                """
                SELECT v.ModelYear, v.Make, v.Model, ci.ItemCode, ci.ItemLabel, d.Finding
                FROM dbo.RepairCase AS rc
                JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
                JOIN dbo.Inspection AS i ON i.InspectionId = rc.InitialInspectionId
                LEFT JOIN dbo.Defect AS d ON d.InspectionId = i.InspectionId
                LEFT JOIN dbo.ChecklistItem AS ci
                    ON ci.ChecklistVersionId = i.ChecklistVersionId AND ci.ItemCode = d.ItemCode
                WHERE rc.RepairCaseId = ?
                ORDER BY d.DefectId;
                """,
                repair_case_id,
            ).fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail="Repair case not found")
            sources = cursor.execute(
                """
                SELECT ResearchSourceId, SourceUrl, VerificationNote
                FROM dbo.RepairResearchSource
                WHERE RepairCaseId = ?
                ORDER BY ResearchSourceId DESC;
                """,
                repair_case_id,
            ).fetchall()

    case = rows[0]
    search_labels = [
        (row.ItemCode, re.sub(r"^\s*lab\s+simulation\s*[:–—-]?\s*", "", row.ItemLabel, flags=re.IGNORECASE))
        for row in rows
        if row.ItemLabel
    ]
    labels = " ".join(dict.fromkeys(label for _, label in search_labels if label))
    arabic_labels = " ".join(dict.fromkeys(
        ARABIC_CHECKLIST_LABELS.get(code, label) for code, label in search_labels if label
    ))
    vehicle = " ".join(str(value) for value in (case.ModelYear, case.Make, case.Model) if value is not None)
    query = f"{vehicle} {labels} diagnosis service bulletin"
    arabic_query = f"{vehicle} {arabic_labels} تشخيص العطل نشرة فنية"
    return RepairResearch(
        suggested_query=" ".join(query.split()),
        suggested_query_ar=" ".join(arabic_query.split()),
        findings=[row.Finding for row in rows if row.Finding],
        sources=[
            ResearchSource(
                research_source_id=row.ResearchSourceId,
                source_url=row.SourceUrl,
                verification_note=row.VerificationNote,
            )
            for row in sources
        ],
    )


@app.post("/repair-cases/{repair_case_id}/research", status_code=201)
def save_repair_research(
    repair_case_id: Annotated[int, Path(gt=0, le=2147483647)],
    source: SaveResearchSource,
) -> ResearchSource:
    url = str(source.source_url)
    note = source.verification_note.strip()
    if len(url) > 2048 or source.source_url.username or source.source_url.password:
        raise HTTPException(status_code=422, detail="Use a public HTTP or HTTPS URL of up to 2048 characters")
    if not note or len(note.encode("utf-16-le")) > 2000:
        raise HTTPException(status_code=422, detail="Enter a verification note of up to 1000 characters")

    with closing(connect_to_database()) as db:
        with closing(db.cursor()) as cursor:
            row = cursor.execute(
                """
                SET NOCOUNT ON;
                INSERT dbo.RepairResearchSource (RepairCaseId, SourceUrl, VerificationNote)
                OUTPUT inserted.ResearchSourceId
                SELECT RepairCaseId, ?, ?
                FROM dbo.RepairCase
                WHERE RepairCaseId = ?;
                """,
                url, note, repair_case_id,
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Repair case not found")
        db.commit()
    return ResearchSource(research_source_id=row.ResearchSourceId, source_url=url, verification_note=note)
