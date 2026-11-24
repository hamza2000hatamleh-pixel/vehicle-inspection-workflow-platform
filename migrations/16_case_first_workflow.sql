USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection' OR COL_LENGTH(N'dbo.RepairAttempt', N'RepairApprovalId') IS NULL
    THROW 50000, 'Run the prior repair approval migrations in VehicleInspection first.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.InspectionDraft', N'IsInitialCaseDraft') IS NULL
BEGIN
    -- The original single-column UNIQUE constraint would permit only one intake with no inspection yet.
    DECLARE @InitialUnique sysname, @InitialOutcomeCheck sysname, @InitialInspectionFk sysname, @Sql nvarchar(max);
    SELECT @InitialUnique = kc.name
    FROM sys.key_constraints AS kc
    JOIN sys.index_columns AS ic
      ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id AND ic.key_ordinal = 1
    WHERE kc.parent_object_id = OBJECT_ID(N'dbo.RepairCase') AND kc.type = 'UQ'
      AND COL_NAME(ic.object_id, ic.column_id) = N'InitialInspectionId'
      AND NOT EXISTS (SELECT 1 FROM sys.index_columns AS extra
                      WHERE extra.object_id = ic.object_id AND extra.index_id = ic.index_id
                        AND extra.key_ordinal > 1);

    SELECT @InitialOutcomeCheck = name FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID(N'dbo.RepairCase')
      AND parent_column_id = COLUMNPROPERTY(parent_object_id, N'InitialOutcome', 'ColumnId');

    SELECT @InitialInspectionFk = fk.name
    FROM sys.foreign_keys AS fk
    JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
    WHERE fk.parent_object_id = OBJECT_ID(N'dbo.RepairCase')
      AND fk.referenced_object_id = OBJECT_ID(N'dbo.Inspection')
      AND COL_NAME(fkc.parent_object_id, fkc.parent_column_id) = N'InitialInspectionId';

    IF @InitialUnique IS NULL OR @InitialOutcomeCheck IS NULL OR @InitialInspectionFk IS NULL
        THROW 50001, 'The original repair case constraints could not be identified.', 1;

    SET @Sql = N'ALTER TABLE dbo.RepairCase DROP CONSTRAINT '
        + QUOTENAME(@InitialUnique) + N', ' + QUOTENAME(@InitialOutcomeCheck)
        + N', ' + QUOTENAME(@InitialInspectionFk) + N', CK_RepairCase_Closure;';
    EXEC sys.sp_executesql @Sql;
    ALTER TABLE dbo.RepairCase ALTER COLUMN InitialInspectionId int NULL;
    ALTER TABLE dbo.RepairCase ALTER COLUMN InitialOutcome varchar(4) NULL;
    CREATE UNIQUE INDEX UX_RepairCase_InitialInspection
        ON dbo.RepairCase (InitialInspectionId) WHERE InitialInspectionId IS NOT NULL;

    ALTER TABLE dbo.RepairCase ADD CONSTRAINT FK_RepairCase_InitialInspection
        FOREIGN KEY (InitialInspectionId, VehicleId, InitialOutcome)
        REFERENCES dbo.Inspection (InspectionId, VehicleId, Outcome),
        CONSTRAINT CK_RepairCase_InitialOutcome
        CHECK (
            (InitialInspectionId IS NULL AND InitialOutcome IS NULL)
            OR (InitialInspectionId IS NOT NULL AND InitialOutcome IS NOT NULL AND InitialOutcome IN ('PASS', 'FAIL'))
        ),
        CONSTRAINT CK_RepairCase_Closure CHECK (
            ClosingOutcome = 'PASS' AND (
                (ClosedAt IS NULL AND ClosingInspectionId IS NULL)
                OR (ClosedAt IS NOT NULL AND InitialInspectionId IS NOT NULL AND (
                    (InitialOutcome = 'PASS' AND ClosingInspectionId IS NULL)
                    OR (InitialOutcome = 'FAIL' AND ClosingInspectionId IS NOT NULL)
                ))
            )
        );

    ALTER TABLE dbo.InspectionDraft ADD IsInitialCaseDraft bit NOT NULL
        CONSTRAINT DF_InspectionDraft_IsInitialCaseDraft DEFAULT 0;
    EXEC sys.sp_executesql N'
        ALTER TABLE dbo.InspectionDraft ADD CONSTRAINT CK_InspectionDraft_InitialCase
            CHECK (IsInitialCaseDraft = 0 OR RepairCaseId IS NOT NULL);
        CREATE UNIQUE INDEX UX_InspectionDraft_InitialCase
            ON dbo.InspectionDraft (RepairCaseId) WHERE IsInitialCaseDraft = 1;';
END;

GRANT INSERT ON dbo.RepairCase TO InspectionApp;

EXEC sys.sp_executesql N'CREATE OR ALTER VIEW dbo.vw_ResumableInspectionDraft
AS
SELECT d.DraftId, d.VehicleId, v.FleetCode, d.ChecklistVersionId, cv.VersionCode,
       d.RepairCaseId, d.CreatedAt, d.Revision, d.Payload, d.IsInitialCaseDraft
FROM dbo.InspectionDraft AS d
JOIN dbo.Vehicle AS v ON v.VehicleId = d.VehicleId
JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = d.ChecklistVersionId
LEFT JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = d.RepairCaseId
WHERE rc.ClosedAt IS NULL
  AND (d.IsInitialCaseDraft = 0 OR rc.InitialInspectionId IS NULL)
  AND NOT EXISTS (SELECT 1 FROM dbo.InspectionSubmission AS s WHERE s.DraftId = d.DraftId);';

EXEC sys.sp_executesql N'CREATE OR ALTER PROCEDURE dbo.FinalizeInspection
    @DraftId uniqueidentifier,
    @ExpectedRevision binary(8),
    @RequestId uniqueidentifier
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @DraftId IS NULL OR @ExpectedRevision IS NULL OR @RequestId IS NULL
        THROW 51005, ''Draft, revision, and request ID are required.'', 1;

    DECLARE @InspectionId int, @CaseId int, @VehicleId int, @VersionId int,
            @Revision binary(8), @StoredRevision binary(8), @StoredDraft uniqueidentifier,
            @Payload nvarchar(max), @RepairNote nvarchar(4000), @Locked bit,
            @Outcome varchar(4), @AttemptNumber int, @LockResult int, @IsInitialCaseDraft bit;

    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC @LockResult = sys.sp_getapplock
            @Resource = N''VehicleInspection.Finalize'', @LockMode = ''Exclusive'',
            @LockOwner = ''Transaction'', @LockTimeout = 10000;
        IF @LockResult < 0
            THROW 51006, ''Finalization is busy. Retry with the same request ID.'', 1;

        SELECT @InspectionId = InspectionId, @StoredDraft = DraftId,
               @StoredRevision = DraftRevision
        FROM dbo.InspectionSubmission WHERE RequestId = @RequestId;

        IF @InspectionId IS NOT NULL
        BEGIN
            IF @StoredDraft <> @DraftId OR @StoredRevision <> @ExpectedRevision
                THROW 51007, ''This request ID belongs to a different draft or revision.'', 1;
        END
        ELSE
        BEGIN
            SELECT @VehicleId = VehicleId, @VersionId = ChecklistVersionId,
                   @CaseId = RepairCaseId, @Payload = Payload, @Revision = Revision,
                   @IsInitialCaseDraft = IsInitialCaseDraft
            FROM dbo.InspectionDraft WITH (UPDLOCK, HOLDLOCK) WHERE DraftId = @DraftId;

            IF @Revision IS NULL OR @Revision <> @ExpectedRevision
                THROW 51008, ''The draft is missing or has changed. Reload it.'', 1;
            IF EXISTS (SELECT 1 FROM dbo.InspectionSubmission WHERE DraftId = @DraftId)
                THROW 51009, ''This draft has already been finalized.'', 1;

            SELECT @Locked = IsLocked FROM dbo.ChecklistVersion WITH (UPDLOCK, HOLDLOCK)
            WHERE ChecklistVersionId = @VersionId;

            IF @CaseId IS NOT NULL
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM dbo.RepairCase WITH (UPDLOCK, HOLDLOCK)
                               WHERE RepairCaseId = @CaseId AND ClosedAt IS NULL)
                    THROW 51010, ''The repair case is closed.'', 1;
                IF @IsInitialCaseDraft = 1 AND EXISTS (
                    SELECT 1 FROM dbo.RepairCase WHERE RepairCaseId = @CaseId AND InitialInspectionId IS NOT NULL
                )
                    THROW 51017, ''The case already has an initial inspection.'', 1;

                IF @IsInitialCaseDraft = 0
                BEGIN
                    SET @RepairNote = TRIM(JSON_VALUE(@Payload, ''$.repairNote''));
                    IF @RepairNote IS NULL OR LEN(@RepairNote) NOT BETWEEN 1 AND 500 OR DATALENGTH(@RepairNote) > 1000
                        THROW 51011, ''Provide a repair note of 1 to 500 characters.'', 1;
                END;
            END;

            DECLARE @Answers TABLE (ItemCode nvarchar(max), Outcome nvarchar(max));
            DECLARE @Defects TABLE (ItemCode nvarchar(max), Severity nvarchar(max), Finding nvarchar(max));

            INSERT @Answers SELECT ItemCode, Outcome
            FROM OPENJSON(@Payload, ''$.answers'') WITH (
                ItemCode nvarchar(max) ''$.itemCode'', Outcome nvarchar(max) ''$.outcome'');
            INSERT @Defects SELECT ItemCode, Severity, Finding
            FROM OPENJSON(@Payload, ''$.defects'') WITH (
                ItemCode nvarchar(max) ''$.itemCode'', Severity nvarchar(max) ''$.severity'',
                Finding nvarchar(max) ''$.finding'');

            IF NOT EXISTS (SELECT 1 FROM dbo.ChecklistItem WHERE ChecklistVersionId = @VersionId)
                OR EXISTS (
                    SELECT 1 FROM @Answers AS a
                    WHERE a.ItemCode IS NULL OR a.Outcome IS NULL OR a.Outcome NOT IN (N''PASS'', N''FAIL'')
                       OR NOT EXISTS (SELECT 1 FROM dbo.ChecklistItem AS ci
                                      WHERE ci.ChecklistVersionId = @VersionId AND ci.ItemCode = a.ItemCode)
                )
                OR EXISTS (
                    SELECT 1 FROM dbo.ChecklistItem AS ci
                    WHERE ci.ChecklistVersionId = @VersionId
                      AND (SELECT COUNT(*) FROM @Answers AS a WHERE a.ItemCode = ci.ItemCode) <> 1
                )
                THROW 51012, ''Provide exactly one valid answer for every checklist item.'', 1;

            IF EXISTS (
                SELECT 1 FROM @Defects AS d
                WHERE d.Severity IS NULL OR d.Severity NOT IN (N''MINOR'', N''MAJOR'', N''CRITICAL'')
                   OR d.Finding IS NULL OR LEN(TRIM(d.Finding)) NOT BETWEEN 1 AND 500
                   OR DATALENGTH(d.Finding) > 1000
                   OR NOT EXISTS (SELECT 1 FROM @Answers AS a
                                  WHERE a.ItemCode = d.ItemCode AND a.Outcome = N''FAIL'')
            ) OR EXISTS (
                SELECT 1 FROM @Answers AS a WHERE a.Outcome = N''FAIL''
                  AND NOT EXISTS (SELECT 1 FROM @Defects AS d WHERE d.ItemCode = a.ItemCode)
            )
                THROW 51013, ''Each failed item needs a valid defect, and defects must reference failed items.'', 1;

            SET @Outcome = CASE WHEN EXISTS (
                SELECT 1 FROM @Answers AS a
                JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                             AND ci.ItemCode = a.ItemCode
                WHERE a.Outcome = N''FAIL'' AND ci.FailInspectionOnFailure = 1
            ) THEN ''FAIL'' ELSE ''PASS'' END;

            IF @Locked = 0
                UPDATE dbo.ChecklistVersion SET IsLocked = 1 WHERE ChecklistVersionId = @VersionId;

            INSERT dbo.Inspection (InspectionCode, VehicleId, ChecklistVersionId, Outcome)
            VALUES (CONCAT(''INSP'', CONVERT(char(36), @DraftId)), @VehicleId, @VersionId, @Outcome);
            SET @InspectionId = CONVERT(int, SCOPE_IDENTITY());

            INSERT dbo.ChecklistResult (InspectionId, ChecklistVersionId, ItemCode, Outcome)
            SELECT @InspectionId, @VersionId, ci.ItemCode,
                   CASE WHEN a.Outcome = N''PASS'' THEN ''PASS'' ELSE ''FAIL'' END
            FROM @Answers AS a
            JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                         AND ci.ItemCode = a.ItemCode;

            INSERT dbo.Defect (InspectionId, ItemCode, Severity, Finding)
            SELECT @InspectionId, ci.ItemCode, UPPER(TRIM(d.Severity)), TRIM(d.Finding)
            FROM @Defects AS d
            JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                         AND ci.ItemCode = d.ItemCode;

            IF @IsInitialCaseDraft = 1
            BEGIN
                UPDATE dbo.RepairCase
                SET InitialInspectionId = @InspectionId, InitialOutcome = @Outcome,
                    ClosedAt = CASE WHEN @Outcome = ''PASS'' THEN SYSUTCDATETIME() ELSE NULL END
                WHERE RepairCaseId = @CaseId;
            END
            ELSE IF @CaseId IS NOT NULL
            BEGIN
                SELECT @AttemptNumber = COALESCE(MAX(AttemptNumber), 0) + 1
                FROM dbo.ReinspectionAttempt WHERE RepairCaseId = @CaseId;

                INSERT dbo.ReinspectionAttempt (RepairCaseId, VehicleId, AttemptNumber, InspectionId, RepairNote)
                VALUES (@CaseId, @VehicleId, @AttemptNumber, @InspectionId, @RepairNote);

                IF @Outcome = ''PASS''
                    UPDATE dbo.RepairCase SET ClosingInspectionId = @InspectionId, ClosedAt = SYSUTCDATETIME()
                    WHERE RepairCaseId = @CaseId;
            END
            ELSE IF @Outcome = ''FAIL''
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
END;';

EXEC sys.sp_executesql N'CREATE OR ALTER TRIGGER dbo.RequireCustomerApprovalForReinspection
ON dbo.ReinspectionAttempt
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;

    -- FinalizeInspection holds the repair case lock through this check.
    IF EXISTS (
        SELECT 1
        FROM inserted AS attempt
        OUTER APPLY (
            SELECT TOP (1) RepairApprovalId, ApprovalStatus
            FROM dbo.RepairApproval
            WHERE RepairCaseId = attempt.RepairCaseId
            ORDER BY RequestedAt DESC, RepairApprovalId DESC
        ) AS current_approval
        WHERE current_approval.ApprovalStatus IS NULL OR current_approval.ApprovalStatus <> ''APPROVED''
    )
        THROW 51016, ''The current repair proposal needs customer approval before reinspection.'', 1;

    IF EXISTS (
        SELECT 1
        FROM inserted AS attempt
        CROSS APPLY (
            SELECT TOP (1) RepairApprovalId
            FROM dbo.RepairApproval
            WHERE RepairCaseId = attempt.RepairCaseId
            ORDER BY RequestedAt DESC, RepairApprovalId DESC
        ) AS current_approval
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.RepairAttempt AS repair
            WHERE repair.RepairCaseId = attempt.RepairCaseId
              AND repair.RepairApprovalId = current_approval.RepairApprovalId
        )
    )
        THROW 51018, ''Log a repair attempt for the current proposal before reinspection.'', 1;
END;';

COMMIT;


