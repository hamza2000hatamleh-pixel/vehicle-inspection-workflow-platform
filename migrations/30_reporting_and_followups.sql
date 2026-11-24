IF DB_NAME() NOT IN (N'VehicleInspection', N'VehicleInspection')
    THROW 50000, 'Select VehicleInspection or VehicleInspection before running this migration.', 1;
IF OBJECT_ID(N'dbo.RepairCase', N'U') IS NULL
   OR OBJECT_ID(N'dbo.RepairApproval', N'U') IS NULL
   OR OBJECT_ID(N'dbo.RepairAttempt', N'U') IS NULL
   OR OBJECT_ID(N'dbo.ReinspectionAttempt', N'U') IS NULL
   OR OBJECT_ID(N'dbo.WorkshopCaseEvent', N'U') IS NULL
    THROW 50000, 'Apply the case, approval, repair, reinspection, and workshop migrations first.', 1;
GO

SET XACT_ABORT ON;
SET NOCOUNT ON;

IF COL_LENGTH(N'dbo.RepairCase', N'IsTestData') IS NULL
    ALTER TABLE dbo.RepairCase ADD IsTestData bit NOT NULL
        CONSTRAINT DF_RepairCase_IsTestData DEFAULT (0) WITH VALUES;
IF COL_LENGTH(N'dbo.RepairCase', N'TestDataFlagChangedAt') IS NULL
    ALTER TABLE dbo.RepairCase ADD TestDataFlagChangedAt datetime2(3) NULL;
IF COL_LENGTH(N'dbo.RepairCase', N'TestDataFlagChangedBy') IS NULL
    ALTER TABLE dbo.RepairCase ADD TestDataFlagChangedBy nvarchar(100) NULL;
GO

SET XACT_ABORT ON;
SET NOCOUNT ON;

UPDATE rc
SET IsTestData = 1,
    TestDataFlagChangedAt = SYSUTCDATETIME(),
    TestDataFlagChangedBy = N'Migration 30 backfill'
FROM dbo.RepairCase AS rc
JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
LEFT JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = initial.ChecklistVersionId
WHERE rc.IsTestData = 0 AND rc.TestDataFlagChangedBy IS NULL
  AND (v.FleetCode LIKE 'SMOKE-%' OR v.FleetCode LIKE 'PILOT-%'
       OR v.FleetCode LIKE 'TEST-%' OR v.FleetCode LIKE 'DEMO-%' OR v.FleetCode LIKE 'QA-%'
       OR cv.VersionCode LIKE 'LAB-%' OR cv.VersionCode LIKE 'QA-%' OR cv.VersionCode LIKE 'DEMO-%');
GO

SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.AdvisorFollowUpContact', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.AdvisorFollowUpContact (
        FollowUpContactId int IDENTITY(1,1) NOT NULL CONSTRAINT PK_AdvisorFollowUpContact PRIMARY KEY,
        RepairCaseId int NOT NULL,
        RepairApprovalId int NOT NULL,
        ContactedAt datetime2(3) NOT NULL CONSTRAINT DF_AdvisorFollowUpContact_ContactedAt DEFAULT SYSUTCDATETIME(),
        ContactMethod varchar(10) NOT NULL,
        ContactResult varchar(16) NOT NULL,
        RecordedBy nvarchar(100) NOT NULL,
        Note nvarchar(500) NULL,
        NextFollowUpAt datetime2(3) NULL,
        CONSTRAINT CK_AdvisorFollowUpContact_Method CHECK (ContactMethod IN ('PHONE','EMAIL','SMS','OTHER')),
        CONSTRAINT CK_AdvisorFollowUpContact_Result CHECK (ContactResult IN ('REACHED','NO_ANSWER','LEFT_MESSAGE','OTHER')),
        CONSTRAINT FK_AdvisorFollowUpContact_Case FOREIGN KEY (RepairCaseId)
            REFERENCES dbo.RepairCase (RepairCaseId),
        CONSTRAINT FK_AdvisorFollowUpContact_Approval FOREIGN KEY (RepairCaseId, RepairApprovalId)
            REFERENCES dbo.RepairApproval (RepairCaseId, RepairApprovalId)
    );
    CREATE INDEX IX_AdvisorFollowUpContact_CaseApproval
        ON dbo.AdvisorFollowUpContact (RepairCaseId, RepairApprovalId, ContactedAt DESC, FollowUpContactId DESC);
END;

GRANT SELECT, INSERT ON dbo.AdvisorFollowUpContact TO InspectionApp;
GRANT UPDATE (IsTestData, TestDataFlagChangedAt, TestDataFlagChangedBy)
    ON dbo.RepairCase TO InspectionApp;

COMMIT;
GO

CREATE OR ALTER VIEW dbo.vw_PBI_RepairCaseSummary
AS
SELECT rc.RepairCaseId, rc.VehicleId, v.FleetCode, v.VIN, v.Make, v.Model, v.ModelYear,
       rc.OpenedAt, rc.ClosedAt, rc.CloseoutReason, rc.CancellationReason,
       rc.IsTestData, CONVERT(bit, CASE WHEN rc.IsTestData = 1 THEN 0 ELSE 1 END) AS IsReportable,
       initial.InspectedAt AS InitialInspectedAt, initial.Outcome AS InitialOutcome,
       proposal.ApprovalStatus AS LatestApprovalStatus, proposal.RequestedAt AS LatestApprovalRequestedAt,
       proposal.DecidedAt AS LatestApprovalDecidedAt,
       repair.PerformedAt AS LatestRepairAt,
       review.InspectionId AS LatestReinspectionId, review.InspectedAt AS LatestReinspectionAt,
       review.Outcome AS LatestReinspectionOutcome, review.AttemptNumber AS LatestReinspectionNumber,
       owner.Assignee AS CurrentAssignee, owner.StaffRole AS CurrentAssigneeRole,
       owner.RecordedAt AS CurrentAssignmentAt,
       CASE WHEN rc.ClosedAt IS NOT NULL THEN 'CLOSED'
            WHEN rc.InitialInspectionId IS NULL THEN 'AWAITING_INSPECTION'
            WHEN proposal.ApprovalStatus IS NULL THEN 'NEEDS_PROPOSAL'
            WHEN proposal.ApprovalStatus = 'PENDING' THEN 'AWAITING_APPROVAL'
            WHEN proposal.ApprovalStatus = 'DECLINED' THEN 'DECLINED'
            WHEN proposal.ApprovalStatus = 'WITHDRAWN' THEN 'WITHDRAWN'
            WHEN repair.PerformedAt IS NULL THEN 'READY_FOR_REPAIR'
            WHEN review.Outcome = 'FAIL' AND ISNULL(owner.InspectionId, -1) <> review.InspectionId THEN 'AWAITING_REASSIGNMENT'
            WHEN review.Outcome = 'FAIL' THEN 'REINSPECTION_FAILED'
            ELSE 'READY_FOR_REINSPECTION' END AS CurrentStage,
       CASE WHEN rc.ClosedAt IS NOT NULL THEN rc.ClosedAt
            WHEN rc.InitialInspectionId IS NULL THEN rc.OpenedAt
            WHEN proposal.ApprovalStatus IS NULL THEN initial.InspectedAt
            WHEN proposal.ApprovalStatus = 'PENDING' THEN proposal.RequestedAt
            WHEN proposal.ApprovalStatus IN ('DECLINED','WITHDRAWN') THEN proposal.DecidedAt
            WHEN repair.PerformedAt IS NULL THEN proposal.DecidedAt
            WHEN review.Outcome = 'FAIL' AND ISNULL(owner.InspectionId, -1) <> review.InspectionId THEN review.InspectedAt
            WHEN review.Outcome = 'FAIL' THEN owner.RecordedAt
            ELSE repair.PerformedAt END AS CurrentStageStartedAt
FROM dbo.RepairCase AS rc
JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
LEFT JOIN dbo.Inspection AS initial ON initial.InspectionId = rc.InitialInspectionId
OUTER APPLY (
    SELECT TOP (1) RepairApprovalId, ApprovalStatus, RequestedAt, DecidedAt
    FROM dbo.RepairApproval WHERE RepairCaseId = rc.RepairCaseId
    ORDER BY RequestedAt DESC, RepairApprovalId DESC
) AS proposal
OUTER APPLY (
    SELECT TOP (1) RepairAttemptId, PerformedAt
    FROM dbo.RepairAttempt
    WHERE RepairCaseId = rc.RepairCaseId AND RepairApprovalId = proposal.RepairApprovalId
    ORDER BY PerformedAt DESC, RepairAttemptId DESC
) AS repair
OUTER APPLY (
    SELECT TOP (1) attempt.AttemptNumber, inspection.InspectionId, inspection.InspectedAt, inspection.Outcome
    FROM dbo.ReinspectionAttempt AS attempt
    JOIN dbo.Inspection AS inspection ON inspection.InspectionId = attempt.InspectionId
    WHERE attempt.RepairCaseId = rc.RepairCaseId
    ORDER BY attempt.AttemptNumber DESC
) AS review
OUTER APPLY (
    SELECT TOP (1) Assignee, StaffRole, InspectionId, RecordedAt
    FROM dbo.WorkshopCaseEvent
    WHERE RepairCaseId = rc.RepairCaseId AND EventKind = 'ASSIGNMENT'
    ORDER BY WorkshopCaseEventId DESC
) AS owner;
GO

CREATE OR ALTER VIEW dbo.vw_PBI_ApprovalIntervals
AS
SELECT approval.RepairApprovalId, approval.RepairCaseId, approval.ApprovalStatus,
       approval.RequestedAt, approval.DecidedAt,
       DATEDIFF_BIG(second, approval.RequestedAt, approval.DecidedAt) AS WaitSeconds,
       caseView.FleetCode, caseView.IsTestData, caseView.IsReportable
FROM dbo.RepairApproval AS approval
JOIN dbo.vw_PBI_RepairCaseSummary AS caseView ON caseView.RepairCaseId = approval.RepairCaseId
WHERE approval.ApprovalStatus IN ('APPROVED','DECLINED') AND approval.DecidedAt IS NOT NULL;
GO

CREATE OR ALTER VIEW dbo.vw_PBI_InspectionIntervals
AS
WITH Reinspections AS (
    SELECT attempt.RepairCaseId, attempt.AttemptNumber, attempt.InspectionId, attempt.VerifiedRepairId,
           inspection.InspectedAt,
           LAG(inspection.InspectedAt) OVER (
               PARTITION BY attempt.RepairCaseId ORDER BY attempt.AttemptNumber
           ) AS PreviousInspectionAt
    FROM dbo.ReinspectionAttempt AS attempt
    JOIN dbo.Inspection AS inspection ON inspection.InspectionId = attempt.InspectionId
)
SELECT rc.RepairCaseId, i.InspectionId, 'INTAKE_TO_INSPECTION' AS Metric,
       i.InspectedAt AS OccurredAt, DATEDIFF_BIG(second, rc.OpenedAt, i.InspectedAt) AS WaitSeconds,
       v.FleetCode, rc.IsTestData, CONVERT(bit, CASE WHEN rc.IsTestData = 1 THEN 0 ELSE 1 END) AS IsReportable
FROM dbo.RepairCase AS rc
JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
JOIN dbo.Inspection AS i ON i.InspectionId = rc.InitialInspectionId
WHERE i.InspectedAt >= rc.OpenedAt
UNION ALL
SELECT rc.RepairCaseId, r.InspectionId, 'REPAIR_TO_REINSPECTION',
       r.InspectedAt, DATEDIFF_BIG(second, repair.PerformedAt, r.InspectedAt),
       v.FleetCode, rc.IsTestData, CONVERT(bit, CASE WHEN rc.IsTestData = 1 THEN 0 ELSE 1 END)
FROM Reinspections AS r
JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = r.RepairCaseId
JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
CROSS APPLY (
    SELECT TOP (1) RepairAttemptId, PerformedAt
    FROM dbo.RepairAttempt
    WHERE RepairCaseId = rc.RepairCaseId AND PerformedAt <= r.InspectedAt
      AND RecordedAt <= r.InspectedAt
      AND (r.VerifiedRepairId IS NULL OR RepairAttemptId = r.VerifiedRepairId)
      AND (r.PreviousInspectionAt IS NULL OR PerformedAt > r.PreviousInspectionAt)
    ORDER BY PerformedAt DESC, RepairAttemptId DESC
) AS repair;
GO

CREATE OR ALTER VIEW dbo.vw_PBI_QCAttempts
AS
SELECT attempt.RepairCaseId, attempt.AttemptNumber, inspection.InspectionId,
       inspection.InspectedAt, inspection.Outcome,
       SUM(CASE WHEN inspection.Outcome = 'FAIL' THEN 1 ELSE 0 END)
           OVER (PARTITION BY attempt.RepairCaseId ORDER BY attempt.AttemptNumber
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS FailedAttemptsToDate,
       v.FleetCode, rc.IsTestData,
       CONVERT(bit, CASE WHEN rc.IsTestData = 1 THEN 0 ELSE 1 END) AS IsReportable
FROM dbo.ReinspectionAttempt AS attempt
JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = attempt.RepairCaseId
JOIN dbo.Vehicle AS v ON v.VehicleId = rc.VehicleId
JOIN dbo.Inspection AS inspection ON inspection.InspectionId = attempt.InspectionId;
GO

IF DATABASE_PRINCIPAL_ID(N'InspectionReportingReader') IS NULL
    EXEC(N'CREATE ROLE InspectionReportingReader AUTHORIZATION dbo;');
GRANT SELECT ON dbo.vw_PBI_RepairCaseSummary TO InspectionReportingReader;
GRANT SELECT ON dbo.vw_PBI_ApprovalIntervals TO InspectionReportingReader;
GRANT SELECT ON dbo.vw_PBI_InspectionIntervals TO InspectionReportingReader;
GRANT SELECT ON dbo.vw_PBI_QCAttempts TO InspectionReportingReader;
GO

-- A DBA can map a separate SQL login to this database role after creating the login:
-- ALTER ROLE InspectionReportingReader ADD MEMBER [YourPowerBIReaderUser];

