USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;

SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.RepairApproval', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RepairApproval (
        RepairApprovalId int IDENTITY PRIMARY KEY,
        RepairCaseId int NOT NULL,
        Diagnosis nvarchar(1000) NOT NULL,
        ProposedRepair nvarchar(1000) NOT NULL,
        RequestedAt datetime2(0) NOT NULL
            CONSTRAINT DF_RepairApproval_RequestedAt DEFAULT SYSUTCDATETIME(),
        RequestedBy nvarchar(100) NOT NULL,
        ApprovalStatus varchar(8) NOT NULL
            CONSTRAINT DF_RepairApproval_Status DEFAULT 'PENDING',
        DecidedAt datetime2(0) NULL,
        DecisionRecordedBy nvarchar(100) NULL,
        DecisionNote nvarchar(500) NULL,

        CONSTRAINT FK_RepairApproval_RepairCase
            FOREIGN KEY (RepairCaseId) REFERENCES dbo.RepairCase (RepairCaseId),
        CONSTRAINT CK_RepairApproval_Details CHECK (
            LEN(LTRIM(RTRIM(Diagnosis))) > 0
            AND LEN(LTRIM(RTRIM(ProposedRepair))) > 0
            AND LEN(LTRIM(RTRIM(RequestedBy))) > 0
        ),
        CONSTRAINT CK_RepairApproval_Decision CHECK (
            (ApprovalStatus = 'PENDING'
                AND DecidedAt IS NULL
                AND DecisionRecordedBy IS NULL
                AND DecisionNote IS NULL)
            OR
            (ApprovalStatus IN ('APPROVED', 'DECLINED')
                AND DecidedAt IS NOT NULL
                AND DecidedAt >= RequestedAt
                AND DecisionRecordedBy IS NOT NULL
                AND LEN(LTRIM(RTRIM(DecisionRecordedBy))) > 0)
        )
    );

    CREATE INDEX IX_RepairApproval_Case
        ON dbo.RepairApproval (RepairCaseId, RequestedAt DESC, RepairApprovalId DESC);
END;

COMMIT;


