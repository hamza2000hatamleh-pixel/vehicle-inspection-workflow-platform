IF DB_ID(N'VehicleInspection') IS NULL
    EXEC(N'CREATE DATABASE VehicleInspection');
GO
USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;

SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.Vehicle', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Vehicle (
        VehicleId int IDENTITY PRIMARY KEY,
        FleetCode varchar(30) NOT NULL UNIQUE,
        ModelYear smallint NOT NULL CHECK (ModelYear BETWEEN 1900 AND 2100),
        Make nvarchar(60) NOT NULL,
        Model nvarchar(60) NOT NULL
    );

    CREATE TABLE dbo.ChecklistVersion (
        ChecklistVersionId int IDENTITY PRIMARY KEY,
        VersionCode varchar(30) NOT NULL UNIQUE,
        Title nvarchar(100) NOT NULL
    );

    CREATE TABLE dbo.ChecklistItem (
        ChecklistVersionId int NOT NULL REFERENCES dbo.ChecklistVersion,
        ItemCode varchar(30) NOT NULL,
        ItemLabel nvarchar(100) NOT NULL,
        FailInspectionOnFailure bit NOT NULL,
        PRIMARY KEY (ChecklistVersionId, ItemCode)
    );

    CREATE TABLE dbo.Inspection (
        InspectionId int IDENTITY PRIMARY KEY,
        InspectionCode varchar(40) NOT NULL UNIQUE,
        VehicleId int NOT NULL REFERENCES dbo.Vehicle,
        ChecklistVersionId int NOT NULL REFERENCES dbo.ChecklistVersion,
        InspectedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        Outcome varchar(4) NOT NULL CHECK (Outcome IN ('PASS', 'FAIL')),
        UNIQUE (InspectionId, ChecklistVersionId),
        UNIQUE (InspectionId, VehicleId),
        UNIQUE (InspectionId, VehicleId, Outcome)
    );

    CREATE TABLE dbo.ChecklistResult (
        InspectionId int NOT NULL,
        ChecklistVersionId int NOT NULL,
        ItemCode varchar(30) NOT NULL,
        Outcome varchar(4) NOT NULL CHECK (Outcome IN ('PASS', 'FAIL')),
        PRIMARY KEY (InspectionId, ItemCode),
        FOREIGN KEY (InspectionId, ChecklistVersionId)
            REFERENCES dbo.Inspection (InspectionId, ChecklistVersionId),
        FOREIGN KEY (ChecklistVersionId, ItemCode)
            REFERENCES dbo.ChecklistItem (ChecklistVersionId, ItemCode)
    );

    CREATE TABLE dbo.Defect (
        DefectId int IDENTITY PRIMARY KEY,
        InspectionId int NOT NULL,
        ItemCode varchar(30) NOT NULL,
        Severity varchar(8) NOT NULL CHECK (Severity IN ('MINOR', 'MAJOR', 'CRITICAL')),
        Finding nvarchar(500) NOT NULL,
        FOREIGN KEY (InspectionId, ItemCode)
            REFERENCES dbo.ChecklistResult (InspectionId, ItemCode)
    );

    CREATE TABLE dbo.RepairCase (
        RepairCaseId int IDENTITY PRIMARY KEY,
        InitialInspectionId int NOT NULL UNIQUE,
        VehicleId int NOT NULL,
        InitialOutcome varchar(4) NOT NULL DEFAULT 'FAIL' CHECK (InitialOutcome = 'FAIL'),
        OpenedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        ClosedAt datetime2(0) NULL,
        CONSTRAINT CK_RepairCase_CloseTime CHECK (ClosedAt IS NULL OR ClosedAt >= OpenedAt),
        UNIQUE (RepairCaseId, VehicleId),
        FOREIGN KEY (InitialInspectionId, VehicleId, InitialOutcome)
            REFERENCES dbo.Inspection (InspectionId, VehicleId, Outcome)
    );

    CREATE TABLE dbo.ReinspectionAttempt (
        RepairCaseId int NOT NULL,
        VehicleId int NOT NULL,
        AttemptNumber int NOT NULL CHECK (AttemptNumber > 0),
        InspectionId int NOT NULL UNIQUE,
        RepairNote nvarchar(500) NOT NULL,
        PRIMARY KEY (RepairCaseId, AttemptNumber),
        FOREIGN KEY (RepairCaseId, VehicleId)
            REFERENCES dbo.RepairCase (RepairCaseId, VehicleId),
        FOREIGN KEY (InspectionId, VehicleId)
            REFERENCES dbo.Inspection (InspectionId, VehicleId)
    );
END;

IF NOT EXISTS (SELECT 1 FROM dbo.Inspection WHERE InspectionCode = 'DEMO-INITIAL-001')
BEGIN
    INSERT dbo.Vehicle (FleetCode, ModelYear, Make, Model)
    VALUES ('DEMO-001', 2018, N'Toyota', N'Corolla');
    DECLARE @VehicleId int = CONVERT(int, SCOPE_IDENTITY());

    INSERT dbo.ChecklistVersion (VersionCode, Title)
    VALUES ('DEMO-1', N'Simulated exterior lighting checklist');
    DECLARE @ChecklistVersionId int = CONVERT(int, SCOPE_IDENTITY());

    INSERT dbo.ChecklistItem (ChecklistVersionId, ItemCode, ItemLabel, FailInspectionOnFailure)
    VALUES (@ChecklistVersionId, 'BRAKE_LIGHT', N'Brake lights', 1),
           (@ChecklistVersionId, 'HEADLIGHT', N'Headlights', 1);

    DECLARE @Observed TABLE (ItemCode varchar(30) PRIMARY KEY, Outcome varchar(4) NOT NULL);
    INSERT @Observed VALUES ('BRAKE_LIGHT', 'FAIL'), ('HEADLIGHT', 'PASS');

    DECLARE @Outcome varchar(4) = CASE WHEN EXISTS (
        SELECT 1
        FROM @Observed AS o
        JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @ChecklistVersionId
                                    AND ci.ItemCode = o.ItemCode
        WHERE o.Outcome = 'FAIL' AND ci.FailInspectionOnFailure = 1
    ) THEN 'FAIL' ELSE 'PASS' END;

    INSERT dbo.Inspection (InspectionCode, VehicleId, ChecklistVersionId, Outcome)
    VALUES ('DEMO-INITIAL-001', @VehicleId, @ChecklistVersionId, @Outcome);
    DECLARE @InspectionId int = CONVERT(int, SCOPE_IDENTITY());

    INSERT dbo.ChecklistResult (InspectionId, ChecklistVersionId, ItemCode, Outcome)
    SELECT @InspectionId, @ChecklistVersionId, ItemCode, Outcome FROM @Observed;

    INSERT dbo.Defect (InspectionId, ItemCode, Severity, Finding)
    VALUES (@InspectionId, 'BRAKE_LIGHT', 'MAJOR', N'Left brake light operates intermittently.');

    IF @Outcome = 'FAIL'
        INSERT dbo.RepairCase (InitialInspectionId, VehicleId)
        VALUES (@InspectionId, @VehicleId);
END;

IF NOT EXISTS (
    SELECT 1
    FROM dbo.Inspection AS i
    JOIN dbo.RepairCase AS rc ON rc.InitialInspectionId = i.InspectionId
    JOIN dbo.Defect AS d ON d.InspectionId = i.InspectionId
    WHERE i.InspectionCode = 'DEMO-INITIAL-001' AND d.ItemCode = 'BRAKE_LIGHT'
)
    THROW 50001, 'The demo inspection must have a brake-light defect and a linked repair case.', 1;

COMMIT;

SELECT v.FleetCode, v.ModelYear, v.Make, v.Model,
       i.InspectionCode, i.Outcome AS InspectionOutcome,
       d.ItemCode, d.Severity, d.Finding, rc.RepairCaseId,
       CASE WHEN rc.ClosedAt IS NULL THEN 'OPEN' ELSE 'CLOSED' END AS CaseStatus
FROM dbo.RepairCase AS rc
JOIN dbo.Inspection AS i ON i.InspectionId = rc.InitialInspectionId
JOIN dbo.Vehicle AS v ON v.VehicleId = i.VehicleId
JOIN dbo.Defect AS d ON d.InspectionId = i.InspectionId
WHERE i.InspectionCode = 'DEMO-INITIAL-001';



