USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;

IF OBJECT_ID(N'dbo.RepairApproval', N'U') IS NULL
    THROW 50000, 'Run 11_customer_repair_approval.sql first.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.RepairAttempt', N'RepairApprovalId') IS NULL
BEGIN
    ALTER TABLE dbo.RepairApproval ADD CONSTRAINT UQ_RepairApproval_Case
        UNIQUE (RepairCaseId, RepairApprovalId);
    ALTER TABLE dbo.RepairAttempt ADD RepairApprovalId int NULL;
    EXEC sys.sp_executesql N'
        ALTER TABLE dbo.RepairAttempt ADD CONSTRAINT FK_RepairAttempt_Approval
            FOREIGN KEY (RepairCaseId, RepairApprovalId)
            REFERENCES dbo.RepairApproval (RepairCaseId, RepairApprovalId);';
END;

GRANT SELECT, INSERT ON dbo.RepairApproval TO InspectionApp;
GRANT UPDATE (ApprovalStatus, DecidedAt, DecisionRecordedBy, DecisionNote)
    ON OBJECT::dbo.RepairApproval TO InspectionApp;

COMMIT;


