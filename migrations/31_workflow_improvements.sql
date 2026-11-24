IF DB_NAME() NOT IN (N'VehicleInspection', N'VehicleInspection')
    THROW 50000, 'Select VehicleInspection or VehicleInspection before running this migration.', 1;
IF OBJECT_ID(N'dbo.RepairCase', N'U') IS NULL
   OR OBJECT_ID(N'dbo.RepairApproval', N'U') IS NULL
   OR OBJECT_ID(N'dbo.RepairAttempt', N'U') IS NULL
   OR OBJECT_ID(N'dbo.ReinspectionAttempt', N'U') IS NULL
   OR OBJECT_ID(N'dbo.WorkshopCaseEvent', N'U') IS NULL
   OR OBJECT_ID(N'dbo.AdvisorFollowUpContact', N'U') IS NULL
    THROW 50000, 'Apply migrations through 30 before this migration.', 1;
GO
SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.RepairCaseConcernLink', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RepairCaseConcernLink (
        ConcernLinkId int IDENTITY(1,1) NOT NULL CONSTRAINT PK_RepairCaseConcernLink PRIMARY KEY,
        RepairCaseId int NOT NULL,
        RelatedRepairCaseId int NOT NULL,
        Relationship varchar(16) NOT NULL,
        RecordedBy nvarchar(100) NOT NULL,
        RecordedAt datetime2(3) NOT NULL CONSTRAINT DF_RepairCaseConcernLink_RecordedAt DEFAULT SYSUTCDATETIME(),
        Note nvarchar(500) NULL,
        CONSTRAINT CK_RepairCaseConcernLink_Relationship CHECK (Relationship IN ('SAME_CONCERN','NEW_CONCERN')),
        CONSTRAINT CK_RepairCaseConcernLink_DifferentCases CHECK (RepairCaseId <> RelatedRepairCaseId),
        CONSTRAINT CK_RepairCaseConcernLink_RecordedBy CHECK (LEN(TRIM(RecordedBy)) > 0),
        CONSTRAINT FK_RepairCaseConcernLink_Current FOREIGN KEY (RepairCaseId) REFERENCES dbo.RepairCase(RepairCaseId),
        CONSTRAINT FK_RepairCaseConcernLink_Related FOREIGN KEY (RelatedRepairCaseId) REFERENCES dbo.RepairCase(RepairCaseId),
        CONSTRAINT UQ_RepairCaseConcernLink UNIQUE (RepairCaseId, RelatedRepairCaseId)
    );
    CREATE INDEX IX_RepairCaseConcernLink_Related ON dbo.RepairCaseConcernLink(RelatedRepairCaseId, RecordedAt DESC);
END;

IF OBJECT_ID(N'dbo.WorkshopAssignmentAcknowledgement', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.WorkshopAssignmentAcknowledgement (
        AcknowledgementId int IDENTITY(1,1) NOT NULL CONSTRAINT PK_WorkshopAssignmentAcknowledgement PRIMARY KEY,
        RepairCaseId int NOT NULL,
        AssignmentEventId int NOT NULL,
        Technician nvarchar(100) NOT NULL,
        Response varchar(10) NOT NULL,
        RecordedAt datetime2(3) NOT NULL CONSTRAINT DF_WorkshopAssignmentAcknowledgement_RecordedAt DEFAULT SYSUTCDATETIME(),
        Note nvarchar(500) NULL,
        CONSTRAINT CK_WorkshopAssignmentAcknowledgement_Response CHECK (Response IN ('ACCEPTED','FLAGGED')),
        CONSTRAINT CK_WorkshopAssignmentAcknowledgement_Technician CHECK (LEN(TRIM(Technician)) > 0),
        CONSTRAINT CK_WorkshopAssignmentAcknowledgement_FlagNote CHECK (Response <> 'FLAGGED' OR LEN(TRIM(Note)) > 0),
        CONSTRAINT FK_WorkshopAssignmentAcknowledgement_Case FOREIGN KEY (RepairCaseId) REFERENCES dbo.RepairCase(RepairCaseId),
        CONSTRAINT FK_WorkshopAssignmentAcknowledgement_Event FOREIGN KEY (AssignmentEventId) REFERENCES dbo.WorkshopCaseEvent(WorkshopCaseEventId)
    );
    CREATE INDEX IX_WorkshopAssignmentAcknowledgement_Event
        ON dbo.WorkshopAssignmentAcknowledgement(AssignmentEventId, RecordedAt DESC, AcknowledgementId DESC);
END;

IF OBJECT_ID(N'dbo.RepairAttemptWorkItem', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RepairAttemptWorkItem (
        RepairAttemptWorkItemId int IDENTITY(1,1) NOT NULL CONSTRAINT PK_RepairAttemptWorkItem PRIMARY KEY,
        RepairAttemptId int NOT NULL,
        WorkType varchar(8) NOT NULL,
        ApprovedScope nvarchar(1000) NOT NULL,
        Description nvarchar(500) NOT NULL,
        PartCode nvarchar(100) NULL,
        Quantity decimal(12,3) NULL,
        LaborMinutes smallint NULL,
        RecordedAt datetime2(3) NOT NULL CONSTRAINT DF_RepairAttemptWorkItem_RecordedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_RepairAttemptWorkItem_WorkType CHECK (WorkType IN ('LABOR','PART')),
        CONSTRAINT CK_RepairAttemptWorkItem_Description CHECK (LEN(TRIM(ApprovedScope)) > 0 AND LEN(TRIM(Description)) > 0),
        CONSTRAINT CK_RepairAttemptWorkItem_Detail CHECK (
            (WorkType = 'LABOR' AND LaborMinutes BETWEEN 1 AND 1440 AND PartCode IS NULL AND Quantity IS NULL)
            OR (WorkType = 'PART' AND PartCode IS NOT NULL AND LEN(TRIM(PartCode)) > 0
                AND Quantity > 0 AND LaborMinutes IS NULL)
        ),
        CONSTRAINT FK_RepairAttemptWorkItem_Attempt FOREIGN KEY (RepairAttemptId) REFERENCES dbo.RepairAttempt(RepairAttemptId)
    );
    CREATE INDEX IX_RepairAttemptWorkItem_Attempt ON dbo.RepairAttemptWorkItem(RepairAttemptId, RepairAttemptWorkItemId);
END;

IF OBJECT_ID(N'dbo.RepairCaseAttachment', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RepairCaseAttachment (
        AttachmentId int IDENTITY(1,1) NOT NULL CONSTRAINT PK_RepairCaseAttachment PRIMARY KEY,
        RepairCaseId int NOT NULL,
        AttachmentScope varchar(12) NOT NULL,
        InspectionId int NULL,
        DefectId int NULL,
        RepairAttemptId int NULL,
        OriginalFileName nvarchar(180) NOT NULL,
        ContentType varchar(32) NOT NULL,
        FileSize int NOT NULL,
        Sha256 binary(32) NOT NULL,
        Caption nvarchar(500) NULL,
        FileBytes varbinary(max) NOT NULL,
        UploadedBy nvarchar(100) NOT NULL,
        UploadedAt datetime2(3) NOT NULL CONSTRAINT DF_RepairCaseAttachment_UploadedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_RepairCaseAttachment_Scope CHECK (
            (AttachmentScope = 'CASE' AND InspectionId IS NULL AND DefectId IS NULL AND RepairAttemptId IS NULL)
            OR (AttachmentScope = 'INSPECTION' AND InspectionId IS NOT NULL AND DefectId IS NULL AND RepairAttemptId IS NULL)
            OR (AttachmentScope = 'DEFECT' AND InspectionId IS NOT NULL AND DefectId IS NOT NULL AND RepairAttemptId IS NULL)
            OR (AttachmentScope = 'REPAIR' AND InspectionId IS NULL AND DefectId IS NULL AND RepairAttemptId IS NOT NULL)
        ),
        CONSTRAINT CK_RepairCaseAttachment_Type CHECK (ContentType IN ('image/jpeg','image/png','image/webp')),
        CONSTRAINT CK_RepairCaseAttachment_Size CHECK (FileSize BETWEEN 1 AND 4194304),
        CONSTRAINT CK_RepairCaseAttachment_Filename CHECK (LEN(TRIM(OriginalFileName)) > 0),
        CONSTRAINT CK_RepairCaseAttachment_UploadedBy CHECK (LEN(TRIM(UploadedBy)) > 0),
        CONSTRAINT FK_RepairCaseAttachment_Case FOREIGN KEY (RepairCaseId) REFERENCES dbo.RepairCase(RepairCaseId),
        CONSTRAINT FK_RepairCaseAttachment_Inspection FOREIGN KEY (InspectionId) REFERENCES dbo.Inspection(InspectionId),
        CONSTRAINT FK_RepairCaseAttachment_Defect FOREIGN KEY (DefectId) REFERENCES dbo.Defect(DefectId),
        CONSTRAINT FK_RepairCaseAttachment_Repair FOREIGN KEY (RepairAttemptId) REFERENCES dbo.RepairAttempt(RepairAttemptId)
    );
    CREATE INDEX IX_RepairCaseAttachment_Case ON dbo.RepairCaseAttachment(RepairCaseId, UploadedAt, AttachmentId);
END;

GRANT SELECT, INSERT ON dbo.RepairCaseConcernLink TO InspectionApp;
GRANT SELECT, INSERT ON dbo.WorkshopAssignmentAcknowledgement TO InspectionApp;
GRANT SELECT, INSERT ON dbo.RepairAttemptWorkItem TO InspectionApp;
GRANT SELECT, INSERT ON dbo.RepairCaseAttachment TO InspectionApp;

COMMIT;
GO


