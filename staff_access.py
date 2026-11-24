"""Optional staff authentication for the shared workshop workspaces."""
import base64
import binascii
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from hmac import compare_digest
import json
from pathlib import Path
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


ROLES = ("advisor", "technician", "quality-control", "supervisor")
ADVISOR_WRITES = (
    ("POST", r"/vehicles"),
    ("PATCH", r"/vehicles/(?:by-id/\d+|[^/]+/powertrain)"),
    ("POST", r"/repair-cases(?:/\d+/(?:close-without-repair|cancel))?"),
    ("PATCH", r"/repair-cases/\d+/(?:intake|approvals/\d+)"),
    ("POST", r"/repair-cases/\d+/approvals/\d+/follow-ups"),
)
TECHNICIAN_WRITES = (
    ("POST", r"/inspection-drafts/[^/]+/(?:finalize|finalize-targeted-failure)"),
    ("PATCH", r"/inspection-drafts/[^/]+"),
    ("POST", r"/repair-cases/\d+/(?:initial-inspection-drafts|approvals"
             r"|approvals/\d+/withdraw|repair-attempts|diagnostic-checks|research)"),
)
QUALITY_WRITES = (
    ("POST", r"/repair-cases/\d+/reinspection-drafts"),
    ("POST", r"/inspection-drafts/[^/]+/(?:finalize|finalize-targeted-failure)"),
    ("PATCH", r"/inspection-drafts/[^/]+"),
)
WORKSHOP_EVENT_ROLES = {
    "ASSIGNMENT": {"supervisor"},
    "PROPOSAL_LINK": {"technician"},
    "FINDING_OUTCOME": {"technician"},
    "ADVISORY_REVIEW": {"supervisor"},
}


@dataclass(frozen=True)
class Staff:
    username: str
    name: str
    role: str
    salt: bytes
    password_hash: bytes


current_staff: ContextVar[Staff | None] = ContextVar("current_staff", default=None)


def password_hash(password: str, salt: bytes) -> bytes:
    return pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)


def load_staff(path: Path) -> dict[str, Staff]:
    if not path.exists():
        return {}
    records = json.loads(path.read_text(encoding="utf-8"))
    staff = {}
    for row in records:
        account = Staff(row["username"], row["name"], row["role"],
                        bytes.fromhex(row["salt"]), bytes.fromhex(row["password_hash"]))
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", account.username)
                or not account.name.strip() or len(account.name) > 100
                or account.role not in ROLES or account.username in staff
                or len(account.salt) != 16 or len(account.password_hash) != 32):
            raise ValueError("Invalid staff account in workshop_staff.json.")
        staff[account.username] = account
    if not {"advisor", "technician", "supervisor"}.issubset(account.role for account in staff.values()):
        raise ValueError("Configure the advisor, technician and supervisor accounts.")
    return staff


def role_allows(role: str, method: str, path: str) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if method == "POST" and re.fullmatch(r"/repair-cases/\d+/workshop-events", path):
        return role in ROLES  # The event kind is checked in the endpoint.
    rules = {"advisor": ADVISOR_WRITES, "technician": TECHNICIAN_WRITES, "quality-control": QUALITY_WRITES,
             "supervisor": (("POST", r"/workshop/technicians"), ("PUT", r"/workshop/technician-shift"),
                            ("PUT", r"/repair-cases/\d+/test-data"))}
    return any(method == verb and re.fullmatch(pattern, path) for verb, pattern in rules.get(role, ()))


class StaffAccess:
    def __init__(self, accounts: dict[str, Staff]):
        self.accounts = accounts

    def authenticate(self, header: str) -> Staff | None:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic" or len(value) > 2048:
            return None
        try:
            username, separator, password = base64.b64decode(value, validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeError):
            return None
        if not separator:
            return None
        account = self.accounts.get(username)
        salt = account.salt if account else bytes(16)
        expected = account.password_hash if account else bytes(32)
        return account if compare_digest(password_hash(password, salt), expected) else None

    async def dispatch(self, request: Request, call_next):
        if not self.accounts or (request.method == "GET" and request.url.path == "/healthz"):
            return await call_next(request)
        actor = await run_in_threadpool(self.authenticate, request.headers.get("Authorization", ""))
        if actor is None:
            return JSONResponse(
                {"detail": "Sign in with your workshop account."}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Vehicle Inspection", charset="UTF-8"'},
            )
        if not role_allows(actor.role, request.method, request.url.path.rstrip("/") or "/"):
            return JSONResponse({"detail": "This action belongs to another workshop role."}, status_code=403)
        token = current_staff.set(actor)
        try:
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            current_staff.reset(token)


def require_workshop_event_role(kind: str):
    actor = current_staff.get()
    if actor and actor.role not in WORKSHOP_EVENT_ROLES[kind]:
        raise HTTPException(status_code=403, detail="This action belongs to another workshop role.")


def audited(model, field: str):
    actor = current_staff.get()
    return model.model_copy(update={field: actor.name}) if actor else model
