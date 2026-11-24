USE VehicleInspection;
GO
SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.RepairAttempt', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RepairAttempt (
        RepairAttemptId int IDENTITY PRIMARY KEY,
        RepairCaseId int NOT NULL REFERENCES dbo.RepairCase,
        RequestId uniqueidentifier NOT NULL,
        PerformedAt datetime2(0) NOT NULL,
        Technician nvarchar(100) NOT NULL CHECK (LEN(TRIM(Technician)) > 0),
        WorkDescription nvarchar(1000) NOT NULL CHECK (LEN(TRIM(WorkDescription)) > 0),
        Outcome varchar(16) NOT NULL CHECK (Outcome IN ('RESOLVED', 'UNRESOLVED', 'INCONCLUSIVE')),
        RecordedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_RepairAttempt_Request UNIQUE (RepairCaseId, RequestId)
    );
    CREATE INDEX IX_RepairAttempt_Timeline
        ON dbo.RepairAttempt (RepairCaseId, PerformedAt, RepairAttemptId);
END;

GRANT SELECT, INSERT ON dbo.RepairAttempt TO InspectionApp;
COMMIT;


