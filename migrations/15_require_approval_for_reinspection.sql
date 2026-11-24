USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;
IF OBJECT_ID(N'dbo.RepairApproval', N'U') IS NULL
    THROW 50000, 'Run 11_customer_repair_approval.sql first.', 1;

EXEC sys.sp_executesql N'
CREATE OR ALTER TRIGGER dbo.RequireCustomerApprovalForReinspection
ON dbo.ReinspectionAttempt
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;

    -- FinalizeInspection holds the repair case lock until the insert and case update commit.
    IF EXISTS (
        SELECT 1
        FROM inserted AS attempt
        OUTER APPLY (
            SELECT TOP (1) ApprovalStatus
            FROM dbo.RepairApproval
            WHERE RepairCaseId = attempt.RepairCaseId
            ORDER BY RequestedAt DESC, RepairApprovalId DESC
        ) AS current_approval
        WHERE current_approval.ApprovalStatus IS NULL
            OR current_approval.ApprovalStatus <> ''APPROVED''
    )
        THROW 51016, ''The current repair proposal needs customer approval before reinspection.'', 1;
END;';


