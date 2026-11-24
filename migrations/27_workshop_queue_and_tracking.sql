USE VehicleInspection;
GO
SET XACT_ABORT ON;
SET NOCOUNT ON;

IF DB_NAME() <> N'VehicleInspection' OR COL_LENGTH(N'dbo.RepairCase', N'CancellationReason') IS NULL
    THROW 50000, 'Apply this migration to VehicleInspection after migration 23.', 1;

BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.WorkshopCaseEvent', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.WorkshopCaseEvent (
        WorkshopCaseEventId int IDENTITY PRIMARY KEY,
        RepairCaseId int NOT NULL REFERENCES dbo.RepairCase (RepairCaseId),
        EventKind varchar(20) NOT NULL,
        RecordedBy nvarchar(100) NOT NULL,
        RecordedAt datetime2(3) NOT NULL CONSTRAINT DF_WorkshopCaseEvent_RecordedAt DEFAULT SYSUTCDATETIME(),
        Assignee nvarchar(100) NULL,
        StaffRole varchar(12) NULL,
        DefectId int NULL REFERENCES dbo.Defect (DefectId),
        RepairApprovalId int NULL REFERENCES dbo.RepairApproval (RepairApprovalId),
        RepairAttemptId int NULL REFERENCES dbo.RepairAttempt (RepairAttemptId),
        InspectionId int NULL REFERENCES dbo.Inspection (InspectionId),
        FindingOutcome varchar(12) NULL,
        Note nvarchar(1000) NULL,
        CONSTRAINT CK_WorkshopCaseEvent_RecordedBy CHECK (LEN(TRIM(RecordedBy)) > 0),
        CONSTRAINT CK_WorkshopCaseEvent_Details CHECK (
            (EventKind = 'ASSIGNMENT' AND DefectId IS NULL AND RepairApprovalId IS NULL AND RepairAttemptId IS NULL
                AND InspectionId IS NULL AND FindingOutcome IS NULL AND Note IS NULL
                AND ((Assignee IS NULL AND StaffRole IS NULL)
                    OR (Assignee IS NOT NULL AND LEN(TRIM(Assignee)) > 0 AND StaffRole IS NOT NULL
                        AND StaffRole IN ('ADVISOR', 'TECHNICIAN'))))
            OR (EventKind = 'PROPOSAL_LINK' AND DefectId IS NOT NULL AND RepairApprovalId IS NOT NULL
                AND RepairAttemptId IS NULL AND InspectionId IS NULL AND FindingOutcome IS NULL
                AND Assignee IS NULL AND StaffRole IS NULL)
            OR (EventKind = 'FINDING_OUTCOME' AND DefectId IS NOT NULL AND RepairApprovalId IS NULL
                AND RepairAttemptId IS NOT NULL AND InspectionId IS NULL AND Assignee IS NULL AND StaffRole IS NULL
                AND FindingOutcome IS NOT NULL AND FindingOutcome IN ('RESOLVED', 'UNRESOLVED', 'INCONCLUSIVE'))
            OR (EventKind = 'ADVISORY_REVIEW' AND InspectionId IS NOT NULL AND DefectId IS NULL
                AND RepairApprovalId IS NULL AND RepairAttemptId IS NULL AND FindingOutcome IS NULL
                AND Assignee IS NULL AND StaffRole IS NULL AND Note IS NOT NULL AND LEN(TRIM(Note)) > 0)
        )
    );

    CREATE INDEX IX_WorkshopCaseEvent_CaseKind
        ON dbo.WorkshopCaseEvent (RepairCaseId, EventKind, WorkshopCaseEventId DESC);
    CREATE UNIQUE INDEX UX_WorkshopCaseEvent_AdvisoryReview
        ON dbo.WorkshopCaseEvent (RepairCaseId, InspectionId) WHERE EventKind = 'ADVISORY_REVIEW';
    CREATE UNIQUE INDEX UX_WorkshopCaseEvent_ProposalLink
        ON dbo.WorkshopCaseEvent (DefectId, RepairApprovalId) WHERE EventKind = 'PROPOSAL_LINK';
    CREATE UNIQUE INDEX UX_WorkshopCaseEvent_FindingOutcome
        ON dbo.WorkshopCaseEvent (DefectId, RepairAttemptId) WHERE EventKind = 'FINDING_OUTCOME';
END;

GRANT SELECT, INSERT ON dbo.WorkshopCaseEvent TO InspectionApp;

COMMIT;


