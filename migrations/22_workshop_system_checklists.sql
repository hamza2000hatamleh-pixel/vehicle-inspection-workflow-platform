USE VehicleInspection;
GO

SET NOCOUNT ON;
SET XACT_ABORT ON;

IF DB_NAME() <> N'VehicleInspection'
    OR OBJECT_ID(N'dbo.ActivePowertrainChecklist', N'U') IS NULL
    OR COL_LENGTH(N'dbo.InspectionDraft', N'VerifiedRepairId') IS NULL
    THROW 50000, 'Apply this migration to VehicleInspection after migration 21.', 1;

BEGIN TRANSACTION;

DECLARE @Versions table (
    Powertrain varchar(6) PRIMARY KEY,
    VersionCode varchar(30) UNIQUE,
    PriorLabCode varchar(30),
    Title nvarchar(100)
);
INSERT @Versions VALUES
    ('ICE', 'WORKSHOP-ICE-1', 'LAB-ICE-1', N'Workshop vehicle systems - combustion'),
    ('HYBRID', 'WORKSHOP-HYBRID-1', 'LAB-HYBRID-1', N'Workshop vehicle systems - hybrid'),
    ('EV', 'WORKSHOP-EV-1', 'LAB-EV-1', N'Workshop vehicle systems - electric');

DECLARE @Checks table (
    Powertrain varchar(6) NOT NULL,
    ItemCode varchar(30) NOT NULL,
    ItemLabel nvarchar(100) NOT NULL,
    Blocking bit NOT NULL,
    PRIMARY KEY (Powertrain, ItemCode)
);
INSERT @Checks VALUES
    ('ALL', 'BRAKES_SERVICE', N'Service and parking brake operation', 1),
    ('ALL', 'STEERING_OPERATION', N'Steering operation and excessive play', 1),
    ('ALL', 'SUSPENSION_CONDITION', N'Suspension components and visible damage', 1),
    ('ALL', 'TIRES_WHEELS', N'Tire condition, pressure and wheel condition', 1),
    ('ALL', 'LIGHTS_EXTERNAL', N'Exterior lights, indicators and hazards', 1),
    ('ALL', 'LOW_VOLTAGE', N'Low-voltage supply and electrical warning indicators', 0),
    ('ALL', 'VISIBILITY_GLASS', N'Windshield, mirrors, wipers and washers', 1),
    ('ALL', 'BODY_STRUCTURE', N'Body, underbody and visible structural damage', 1),
    ('ALL', 'BODY_LATCHES', N'Doors and access panel latches', 1),
    ('ALL', 'RESTRAINTS_BELTS', N'Seats, seat belts and restraint warning indicators', 1),
    ('ALL', 'CLIMATE_VENTILATION', N'Cabin ventilation and climate controls', 0),
    ('ALL', 'DRIVETRAIN_OPERATION', N'Drive selection and drivetrain warning indicators', 0),
    ('ALL', 'INSTRUMENTS_DISPLAY', N'Driver display, speed indication and warning lamps', 0),
    ('ICE', 'ENGINE_COOLING', N'Engine operation, oil and coolant condition', 0),
    ('ICE', 'FUEL_EXHAUST', N'Visible fuel leaks and exhaust condition', 1),
    ('HYBRID', 'ENGINE_COOLING', N'Engine operation, oil and coolant condition', 0),
    ('HYBRID', 'FUEL_EXHAUST', N'Visible fuel leaks and exhaust condition', 1),
    ('HYBRID', 'HV_INDICATORS', N'Traction battery and high-voltage warning indicators', 0),
    ('HYBRID', 'HYBRID_OPERATION', N'Hybrid drive operation and system warnings', 0),
    ('EV', 'HV_INDICATORS', N'Traction battery and high-voltage warning indicators', 0),
    ('EV', 'EV_CHARGE_PORT', N'External charge port condition and charging indications', 0),
    ('EV', 'EV_THERMAL', N'Electric powertrain thermal-management warnings', 0);

DECLARE @Created table (
    ChecklistVersionId int PRIMARY KEY,
    VersionCode varchar(30) UNIQUE
);

-- A pre-existing custom checklist may not use one of these version codes.
IF EXISTS (
    SELECT 1
    FROM @Versions AS expected
    JOIN dbo.ChecklistVersion AS actual WITH (UPDLOCK, HOLDLOCK)
        ON actual.VersionCode = expected.VersionCode
    WHERE actual.Title <> expected.Title
)
    THROW 50001, 'A workshop checklist version code is already in use.', 1;

INSERT dbo.ChecklistVersion (VersionCode, Title)
OUTPUT inserted.ChecklistVersionId, inserted.VersionCode INTO @Created
SELECT v.VersionCode, v.Title
FROM @Versions AS v
WHERE NOT EXISTS (
    SELECT 1
    FROM dbo.ChecklistVersion AS currentVersion WITH (UPDLOCK, HOLDLOCK)
    WHERE currentVersion.VersionCode = v.VersionCode
);

INSERT dbo.ChecklistItem (ChecklistVersionId, ItemCode, ItemLabel, FailInspectionOnFailure)
SELECT created.ChecklistVersionId, checks.ItemCode, checks.ItemLabel, checks.Blocking
FROM @Created AS created
JOIN @Versions AS v ON v.VersionCode = created.VersionCode
JOIN @Checks AS checks ON checks.Powertrain IN ('ALL', v.Powertrain);

-- Replace lab defaults only. An existing workshop-specific assignment takes precedence.
UPDATE active
SET ChecklistVersionId = currentVersion.ChecklistVersionId
FROM dbo.ActivePowertrainChecklist AS active
JOIN @Versions AS v ON v.Powertrain = active.Powertrain
JOIN dbo.ChecklistVersion AS prior ON prior.ChecklistVersionId = active.ChecklistVersionId
JOIN dbo.ChecklistVersion AS currentVersion ON currentVersion.VersionCode = v.VersionCode
WHERE prior.VersionCode = v.PriorLabCode;

INSERT dbo.ActivePowertrainChecklist (Powertrain, ChecklistVersionId)
SELECT v.Powertrain, currentVersion.ChecklistVersionId
FROM @Versions AS v
JOIN dbo.ChecklistVersion AS currentVersion ON currentVersion.VersionCode = v.VersionCode
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.ActivePowertrainChecklist AS active WITH (UPDLOCK, HOLDLOCK)
    WHERE active.Powertrain = v.Powertrain
);

COMMIT;

SELECT active.Powertrain, v.VersionCode, v.Title, COUNT(i.ItemCode) AS CheckCount
FROM dbo.ActivePowertrainChecklist AS active
JOIN dbo.ChecklistVersion AS v ON v.ChecklistVersionId = active.ChecklistVersionId
LEFT JOIN dbo.ChecklistItem AS i ON i.ChecklistVersionId = v.ChecklistVersionId
GROUP BY active.Powertrain, v.VersionCode, v.Title
ORDER BY active.Powertrain;


