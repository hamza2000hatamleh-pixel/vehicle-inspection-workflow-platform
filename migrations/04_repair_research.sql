USE VehicleInspection;
GO
SET XACT_ABORT ON;
IF DB_NAME() <> N'VehicleInspection' OR OBJECT_ID(N'dbo.RepairCase', N'U') IS NULL
    THROW 53000, 'Run the inspection database setup first.', 1;
IF DATABASE_PRINCIPAL_ID(N'InspectionApp') IS NULL
    THROW 53001, 'Run 02_harden_inspection_workflow.sql first.', 1;

BEGIN TRANSACTION;
IF OBJECT_ID(N'dbo.RepairResearchSource', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RepairResearchSource (
        ResearchSourceId int IDENTITY PRIMARY KEY,
        RepairCaseId int NOT NULL REFERENCES dbo.RepairCase,
        SourceUrl nvarchar(2048) NOT NULL,
        VerificationNote nvarchar(1000) NOT NULL CHECK (LEN(TRIM(VerificationNote)) > 0),
        SavedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        SavedBy sysname NOT NULL DEFAULT ORIGINAL_LOGIN()
    );
    CREATE INDEX IX_RepairResearchSource_Case
        ON dbo.RepairResearchSource (RepairCaseId, ResearchSourceId DESC);
END;

GRANT SELECT, INSERT ON dbo.RepairResearchSource TO InspectionApp;
COMMIT;


