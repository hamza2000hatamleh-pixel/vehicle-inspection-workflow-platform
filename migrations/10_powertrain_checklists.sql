USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;

SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.Vehicle', N'Powertrain') IS NULL
    ALTER TABLE dbo.Vehicle ADD Powertrain varchar(6) NULL
        CONSTRAINT CK_Vehicle_Powertrain CHECK (Powertrain IN ('EV', 'ICE', 'HYBRID'));

IF OBJECT_ID(N'dbo.ActivePowertrainChecklist', N'U') IS NULL
    CREATE TABLE dbo.ActivePowertrainChecklist (
        Powertrain varchar(6) PRIMARY KEY CHECK (Powertrain IN ('EV', 'ICE', 'HYBRID')),
        ChecklistVersionId int NOT NULL UNIQUE REFERENCES dbo.ChecklistVersion
    );

-- Compile after the new column and table exist.
EXEC sys.sp_executesql N'
    UPDATE dbo.Vehicle SET Powertrain = ''ICE''
    WHERE FleetCode = ''DEMO-001'' AND ModelYear = 2018
      AND Make = N''Toyota'' AND Model = N''Corolla'' AND Powertrain IS NULL;

    GRANT UPDATE (Powertrain) ON OBJECT::dbo.Vehicle TO InspectionApp;
    GRANT SELECT ON dbo.ActivePowertrainChecklist TO InspectionApp;
';

DECLARE @LabChecklists table (
    Powertrain varchar(6) PRIMARY KEY,
    VersionCode varchar(30),
    Title nvarchar(100),
    ItemCode varchar(30),
    ItemLabel nvarchar(100)
);
INSERT @LabChecklists VALUES
    ('EV', 'LAB-EV-1', N'LAB ONLY - EV checklist', 'EV_DISPLAY', N'Lab simulation: EV charge display'),
    ('ICE', 'LAB-ICE-1', N'LAB ONLY - ICE checklist', 'ICE_DISPLAY', N'Lab simulation: engine indicator'),
    ('HYBRID', 'LAB-HYBRID-1', N'LAB ONLY - HYBRID checklist', 'HYBRID_DISPLAY', N'Lab simulation: hybrid system indicator');

IF EXISTS (
    SELECT 1 FROM @LabChecklists AS lab
    JOIN dbo.ChecklistVersion AS cv ON cv.VersionCode = lab.VersionCode
    WHERE cv.Title <> lab.Title
)
    THROW 50001, 'A lab checklist code is already in use.', 1;

DECLARE @NewVersions table (VersionCode varchar(30), ChecklistVersionId int);
INSERT dbo.ChecklistVersion (VersionCode, Title)
OUTPUT inserted.VersionCode, inserted.ChecklistVersionId INTO @NewVersions
SELECT lab.VersionCode, lab.Title FROM @LabChecklists AS lab
WHERE NOT EXISTS (SELECT 1 FROM dbo.ChecklistVersion AS cv WHERE cv.VersionCode = lab.VersionCode);

INSERT dbo.ChecklistItem (ChecklistVersionId, ItemCode, ItemLabel, FailInspectionOnFailure)
SELECT v.ChecklistVersionId, 'LAB_LIGHT', N'Lab simulation: exterior light', 1 FROM @NewVersions AS v
UNION ALL
SELECT v.ChecklistVersionId, lab.ItemCode, lab.ItemLabel, 1
FROM @NewVersions AS v JOIN @LabChecklists AS lab ON lab.VersionCode = v.VersionCode;

INSERT dbo.ActivePowertrainChecklist (Powertrain, ChecklistVersionId)
SELECT lab.Powertrain, cv.ChecklistVersionId FROM @LabChecklists AS lab
JOIN dbo.ChecklistVersion AS cv ON cv.VersionCode = lab.VersionCode AND cv.Title = lab.Title
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.ActivePowertrainChecklist AS active WHERE active.Powertrain = lab.Powertrain
);

COMMIT;


