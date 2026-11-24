USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection' OR COL_LENGTH(N'dbo.RepairCase', N'OdometerMiles') IS NULL
    THROW 50000, 'Run 19_odometer_miles.sql first.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.DiagnosticCheck', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.DiagnosticCheck (
        DiagnosticCheckId int IDENTITY PRIMARY KEY,
        RepairCaseId int NOT NULL REFERENCES dbo.RepairCase (RepairCaseId),
        CheckPerformed nvarchar(2000) NOT NULL,
        Observation nvarchar(2000) NOT NULL,
        Technician nvarchar(200) NULL,
        RecordedAt datetime2(3) NOT NULL
            CONSTRAINT DF_DiagnosticCheck_RecordedAt DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_DiagnosticCheck_Case
        ON dbo.DiagnosticCheck (RepairCaseId, RecordedAt, DiagnosticCheckId);
END;

GRANT SELECT, INSERT ON OBJECT::dbo.DiagnosticCheck TO InspectionApp;

COMMIT;


