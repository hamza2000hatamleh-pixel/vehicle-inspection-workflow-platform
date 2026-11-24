USE VehicleInspection;
GO
SET XACT_ABORT ON;

IF DB_NAME() <> N'VehicleInspection' OR OBJECT_ID(N'dbo.ReinspectionAttempt', N'U') IS NULL
    THROW 51000, 'Run 01_setup_demo.sql first.', 1;

BEGIN TRANSACTION;
IF OBJECT_ID(N'dbo.InspectionDraft', N'U') IS NULL
BEGIN
    ALTER TABLE dbo.ChecklistVersion ADD IsLocked bit NOT NULL
        CONSTRAINT DF_ChecklistVersion_IsLocked DEFAULT (0);
    EXEC(N'UPDATE cv SET IsLocked = 1
    FROM dbo.ChecklistVersion AS cv
    WHERE EXISTS (SELECT 1 FROM dbo.Inspection AS i
                  WHERE i.ChecklistVersionId = cv.ChecklistVersionId);');

    ALTER TABLE dbo.RepairCase ADD ClosingInspectionId int NULL,
        ClosingOutcome varchar(4) NOT NULL CONSTRAINT DF_RepairCase_ClosingOutcome DEFAULT ('PASS');
    ALTER TABLE dbo.ReinspectionAttempt ADD CONSTRAINT UQ_Reinspection_CaseInspection
        UNIQUE (RepairCaseId, InspectionId);

    EXEC(N'-- Backfill existing closed cases only when a passing attempt can be identified.
    UPDATE rc SET ClosingInspectionId = closure.InspectionId
    FROM dbo.RepairCase AS rc
    CROSS APPLY (
        SELECT TOP (1) ra.InspectionId
        FROM dbo.ReinspectionAttempt AS ra
        JOIN dbo.Inspection AS i ON i.InspectionId = ra.InspectionId
        WHERE ra.RepairCaseId = rc.RepairCaseId AND i.Outcome = ''PASS''
          AND i.InspectedAt <= rc.ClosedAt
        ORDER BY ra.AttemptNumber DESC
    ) AS closure
    WHERE rc.ClosedAt IS NOT NULL;

    IF EXISTS (SELECT 1 FROM dbo.RepairCase
               WHERE ClosedAt IS NOT NULL AND ClosingInspectionId IS NULL)
        THROW 51001, ''An existing closed case has no passing attempt to backfill.'', 1;

    ALTER TABLE dbo.RepairCase ADD CONSTRAINT CK_RepairCase_Closure
        CHECK (ClosingOutcome = ''PASS'' AND ((ClosingInspectionId IS NULL AND ClosedAt IS NULL) OR
               (ClosingInspectionId IS NOT NULL AND ClosedAt IS NOT NULL))),
        CONSTRAINT FK_RepairCase_ClosingAttempt
            FOREIGN KEY (RepairCaseId, ClosingInspectionId)
            REFERENCES dbo.ReinspectionAttempt (RepairCaseId, InspectionId),
        CONSTRAINT FK_RepairCase_PassingInspection
            FOREIGN KEY (ClosingInspectionId, VehicleId, ClosingOutcome)
            REFERENCES dbo.Inspection (InspectionId, VehicleId, Outcome);');

    CREATE TABLE dbo.InspectionDraft (
        DraftId uniqueidentifier PRIMARY KEY DEFAULT NEWSEQUENTIALID(),
        VehicleId int NOT NULL REFERENCES dbo.Vehicle,
        ChecklistVersionId int NOT NULL REFERENCES dbo.ChecklistVersion,
        RepairCaseId int NULL,
        Payload nvarchar(max) NOT NULL DEFAULT N'{}' CHECK (ISJSON(Payload) = 1),
        CreatedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        Revision rowversion NOT NULL,
        FOREIGN KEY (RepairCaseId, VehicleId) REFERENCES dbo.RepairCase (RepairCaseId, VehicleId)
    );

    CREATE TABLE dbo.InspectionSubmission (
        RequestId uniqueidentifier PRIMARY KEY,
        DraftId uniqueidentifier NOT NULL UNIQUE REFERENCES dbo.InspectionDraft,
        DraftRevision binary(8) NOT NULL,
        InspectionId int NOT NULL UNIQUE REFERENCES dbo.Inspection,
        RepairCaseId int NULL REFERENCES dbo.RepairCase,
        FinalizedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        FinalizedBy sysname NOT NULL DEFAULT ORIGINAL_LOGIN()
    );

    CREATE TABLE dbo.InspectionCorrection (
        CorrectionId int IDENTITY PRIMARY KEY,
        InspectionId int NOT NULL REFERENCES dbo.Inspection,
        Reason nvarchar(500) NOT NULL CHECK (LEN(TRIM(Reason)) > 0),
        CorrectionNote nvarchar(2000) NOT NULL CHECK (LEN(TRIM(CorrectionNote)) > 0),
        RecordedAt datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
        RecordedBy sysname NOT NULL DEFAULT ORIGINAL_LOGIN()
    );
END;
COMMIT;
GO

CREATE OR ALTER TRIGGER dbo.ProtectLockedChecklistVersion
ON dbo.ChecklistVersion
AFTER UPDATE
AS
BEGIN
    IF EXISTS (SELECT 1 FROM deleted WHERE IsLocked = 1)
        THROW 51002, 'A locked checklist version cannot be changed.', 1;
END;
GO

CREATE OR ALTER TRIGGER dbo.ProtectLockedChecklistItems
ON dbo.ChecklistItem
AFTER INSERT, UPDATE, DELETE
AS
BEGIN
    IF EXISTS (
        SELECT 1 FROM dbo.ChecklistVersion AS cv WITH (UPDLOCK, HOLDLOCK)
        JOIN (SELECT ChecklistVersionId FROM inserted
              UNION SELECT ChecklistVersionId FROM deleted) AS changed
          ON changed.ChecklistVersionId = cv.ChecklistVersionId
        WHERE cv.IsLocked = 1
    )
        THROW 51003, 'Create a new checklist version to change locked items.', 1;
END;
GO

CREATE OR ALTER TRIGGER dbo.ProtectFinalizedDraft
ON dbo.InspectionDraft
AFTER UPDATE
AS
BEGIN
    IF EXISTS (SELECT 1 FROM inserted AS i
               JOIN dbo.InspectionSubmission AS s ON s.DraftId = i.DraftId)
        THROW 51004, 'A finalized draft cannot be changed.', 1;
END;
GO

CREATE OR ALTER PROCEDURE dbo.FinalizeInspection
    @DraftId uniqueidentifier,
    @ExpectedRevision binary(8),
    @RequestId uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @DraftId IS NULL OR @ExpectedRevision IS NULL OR @RequestId IS NULL
        THROW 51005, 'Draft, revision, and request ID are required.', 1;

    DECLARE @InspectionId int, @CaseId int, @VehicleId int, @VersionId int,
            @Revision binary(8), @StoredRevision binary(8), @StoredDraft uniqueidentifier,
            @Payload nvarchar(max), @RepairNote nvarchar(4000), @Locked bit,
            @Outcome varchar(4), @AttemptNumber int, @LockResult int;

    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC @LockResult = sys.sp_getapplock
            @Resource = N'VehicleInspection.Finalize', @LockMode = 'Exclusive',
            @LockOwner = 'Transaction', @LockTimeout = 10000;
        IF @LockResult < 0
            THROW 51006, 'Finalization is busy. Retry with the same request ID.', 1;

        SELECT @InspectionId = InspectionId, @StoredDraft = DraftId,
               @StoredRevision = DraftRevision
        FROM dbo.InspectionSubmission WHERE RequestId = @RequestId;

        IF @InspectionId IS NOT NULL
        BEGIN
            IF @StoredDraft <> @DraftId OR @StoredRevision <> @ExpectedRevision
                THROW 51007, 'This request ID belongs to a different draft or revision.', 1;
        END
        ELSE
        BEGIN
            SELECT @VehicleId = VehicleId, @VersionId = ChecklistVersionId,
                   @CaseId = RepairCaseId, @Payload = Payload, @Revision = Revision
            FROM dbo.InspectionDraft WITH (UPDLOCK, HOLDLOCK) WHERE DraftId = @DraftId;

            IF @Revision IS NULL OR @Revision <> @ExpectedRevision
                THROW 51008, 'The draft is missing or has changed. Reload it.', 1;
            IF EXISTS (SELECT 1 FROM dbo.InspectionSubmission WHERE DraftId = @DraftId)
                THROW 51009, 'This draft has already been finalized.', 1;

            SELECT @Locked = IsLocked FROM dbo.ChecklistVersion WITH (UPDLOCK, HOLDLOCK)
            WHERE ChecklistVersionId = @VersionId;

            IF @CaseId IS NOT NULL
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM dbo.RepairCase WITH (UPDLOCK, HOLDLOCK)
                               WHERE RepairCaseId = @CaseId AND ClosedAt IS NULL)
                    THROW 51010, 'The repair case is closed.', 1;
                SET @RepairNote = TRIM(JSON_VALUE(@Payload, '$.repairNote'));
                IF @RepairNote IS NULL OR LEN(@RepairNote) NOT BETWEEN 1 AND 500 OR DATALENGTH(@RepairNote) > 1000
                    THROW 51011, 'Provide a repair note of 1 to 500 characters.', 1;
            END;

            DECLARE @Answers TABLE (ItemCode nvarchar(max), Outcome nvarchar(max));
            DECLARE @Defects TABLE (ItemCode nvarchar(max), Severity nvarchar(max), Finding nvarchar(max));

            INSERT @Answers SELECT ItemCode, Outcome
            FROM OPENJSON(@Payload, '$.answers') WITH (
                ItemCode nvarchar(max) '$.itemCode', Outcome nvarchar(max) '$.outcome');
            INSERT @Defects SELECT ItemCode, Severity, Finding
            FROM OPENJSON(@Payload, '$.defects') WITH (
                ItemCode nvarchar(max) '$.itemCode', Severity nvarchar(max) '$.severity',
                Finding nvarchar(max) '$.finding');

            IF NOT EXISTS (SELECT 1 FROM dbo.ChecklistItem WHERE ChecklistVersionId = @VersionId)
                OR EXISTS (
                    SELECT 1 FROM @Answers AS a
                    WHERE a.ItemCode IS NULL OR a.Outcome IS NULL OR a.Outcome NOT IN (N'PASS', N'FAIL')
                       OR NOT EXISTS (SELECT 1 FROM dbo.ChecklistItem AS ci
                                      WHERE ci.ChecklistVersionId = @VersionId AND ci.ItemCode = a.ItemCode)
                )
                OR EXISTS (
                    SELECT 1 FROM dbo.ChecklistItem AS ci
                    WHERE ci.ChecklistVersionId = @VersionId
                      AND (SELECT COUNT(*) FROM @Answers AS a WHERE a.ItemCode = ci.ItemCode) <> 1
                )
                THROW 51012, 'Provide exactly one valid answer for every checklist item.', 1;

            IF EXISTS (
                SELECT 1 FROM @Defects AS d
                WHERE d.Severity IS NULL OR d.Severity NOT IN (N'MINOR', N'MAJOR', N'CRITICAL')
                   OR d.Finding IS NULL OR LEN(TRIM(d.Finding)) NOT BETWEEN 1 AND 500
                   OR DATALENGTH(d.Finding) > 1000
                   OR NOT EXISTS (SELECT 1 FROM @Answers AS a
                                  WHERE a.ItemCode = d.ItemCode AND a.Outcome = N'FAIL')
            ) OR EXISTS (
                SELECT 1 FROM @Answers AS a WHERE a.Outcome = N'FAIL'
                  AND NOT EXISTS (SELECT 1 FROM @Defects AS d WHERE d.ItemCode = a.ItemCode)
            )
                THROW 51013, 'Each failed item needs a valid defect, and defects must reference failed items.', 1;

            SET @Outcome = CASE WHEN EXISTS (
                SELECT 1 FROM @Answers AS a
                JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                             AND ci.ItemCode = a.ItemCode
                WHERE a.Outcome = N'FAIL' AND ci.FailInspectionOnFailure = 1
            ) THEN 'FAIL' ELSE 'PASS' END;

            IF @Locked = 0
                UPDATE dbo.ChecklistVersion SET IsLocked = 1 WHERE ChecklistVersionId = @VersionId;

            INSERT dbo.Inspection (InspectionCode, VehicleId, ChecklistVersionId, Outcome)
            VALUES (CONCAT('INSP', CONVERT(char(36), @DraftId)), @VehicleId, @VersionId, @Outcome);
            SET @InspectionId = CONVERT(int, SCOPE_IDENTITY());

            INSERT dbo.ChecklistResult (InspectionId, ChecklistVersionId, ItemCode, Outcome)
            SELECT @InspectionId, @VersionId, ci.ItemCode,
                   CASE WHEN a.Outcome = N'PASS' THEN 'PASS' ELSE 'FAIL' END
            FROM @Answers AS a
            JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                         AND ci.ItemCode = a.ItemCode;

            INSERT dbo.Defect (InspectionId, ItemCode, Severity, Finding)
            SELECT @InspectionId, ci.ItemCode, UPPER(TRIM(d.Severity)), TRIM(d.Finding)
            FROM @Defects AS d
            JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                         AND ci.ItemCode = d.ItemCode;

            IF @CaseId IS NOT NULL
            BEGIN
                SELECT @AttemptNumber = COALESCE(MAX(AttemptNumber), 0) + 1
                FROM dbo.ReinspectionAttempt WHERE RepairCaseId = @CaseId;

                INSERT dbo.ReinspectionAttempt (RepairCaseId, VehicleId, AttemptNumber, InspectionId, RepairNote)
                VALUES (@CaseId, @VehicleId, @AttemptNumber, @InspectionId, @RepairNote);

                IF @Outcome = 'PASS'
                    UPDATE dbo.RepairCase SET ClosingInspectionId = @InspectionId, ClosedAt = SYSUTCDATETIME()
                    WHERE RepairCaseId = @CaseId;
            END
            ELSE IF @Outcome = 'FAIL'
            BEGIN
                INSERT dbo.RepairCase (InitialInspectionId, VehicleId) VALUES (@InspectionId, @VehicleId);
                SET @CaseId = CONVERT(int, SCOPE_IDENTITY());
            END;

            INSERT dbo.InspectionSubmission (RequestId, DraftId, DraftRevision, InspectionId, RepairCaseId)
            VALUES (@RequestId, @DraftId, @Revision, @InspectionId, @CaseId);
        END;
        COMMIT;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK;
        THROW;
    END CATCH;

    SELECT s.RequestId, s.DraftId, s.InspectionId, i.Outcome, s.RepairCaseId,
           s.FinalizedAt, s.FinalizedBy
    FROM dbo.InspectionSubmission AS s
    JOIN dbo.Inspection AS i ON i.InspectionId = s.InspectionId
    WHERE s.RequestId = @RequestId;
END;
GO

CREATE OR ALTER PROCEDURE dbo.RecordInspectionCorrection
    @InspectionId int,
    @Reason nvarchar(max),
    @CorrectionNote nvarchar(max)
AS
BEGIN
    SET NOCOUNT ON;
    IF @Reason IS NULL OR @CorrectionNote IS NULL
       OR LEN(TRIM(@Reason)) = 0 OR DATALENGTH(TRIM(@Reason)) > 1000
       OR LEN(TRIM(@CorrectionNote)) = 0 OR DATALENGTH(TRIM(@CorrectionNote)) > 4000
        THROW 51014, 'Provide a reason and a correction note within the field limits.', 1;

    INSERT dbo.InspectionCorrection (InspectionId, Reason, CorrectionNote)
    OUTPUT inserted.CorrectionId, inserted.RecordedAt, inserted.RecordedBy
    VALUES (@InspectionId, TRIM(@Reason), TRIM(@CorrectionNote));
END;
GO

IF DATABASE_PRINCIPAL_ID(N'InspectionApp') IS NULL CREATE ROLE InspectionApp;
GRANT SELECT ON dbo.Vehicle TO InspectionApp;
GRANT SELECT ON dbo.ChecklistVersion TO InspectionApp;
GRANT SELECT ON dbo.ChecklistItem TO InspectionApp;
GRANT SELECT ON dbo.Inspection TO InspectionApp;
GRANT SELECT ON dbo.ChecklistResult TO InspectionApp;
GRANT SELECT ON dbo.Defect TO InspectionApp;
GRANT SELECT ON dbo.RepairCase TO InspectionApp;
GRANT SELECT ON dbo.ReinspectionAttempt TO InspectionApp;
GRANT SELECT ON dbo.InspectionCorrection TO InspectionApp;
GRANT SELECT, INSERT ON dbo.InspectionDraft TO InspectionApp;
GRANT UPDATE (Payload) ON OBJECT::dbo.InspectionDraft TO InspectionApp;
GRANT EXECUTE ON dbo.FinalizeInspection TO InspectionApp;
GRANT EXECUTE ON dbo.RecordInspectionCorrection TO InspectionApp;
GO



