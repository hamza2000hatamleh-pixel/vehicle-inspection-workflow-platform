USE VehicleInspection;
GO

SET XACT_ABORT ON;
SET NOCOUNT ON;

IF DB_NAME() <> N'VehicleInspection'
    OR COL_LENGTH(N'dbo.InspectionDraft', N'VerifiedRepairId') IS NULL
    THROW 50000, 'Apply this migration to VehicleInspection after migration 21.', 1;

EXEC sys.sp_executesql N'CREATE OR ALTER PROCEDURE dbo.FinalizeInspection
    @DraftId uniqueidentifier,
    @ExpectedRevision binary(8),
    @RequestId uniqueidentifier,
    @TargetedFailure bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @DraftId IS NULL OR @ExpectedRevision IS NULL OR @RequestId IS NULL
        THROW 51005, ''Draft, revision, and request ID are required.'', 1;

    DECLARE @InspectionId int, @CaseId int, @VehicleId int, @VersionId int,
            @Revision binary(8), @StoredRevision binary(8), @StoredDraft uniqueidentifier,
            @Payload nvarchar(max), @RepairNote nvarchar(4000), @Locked bit,
            @Outcome varchar(4), @AttemptNumber int, @LockResult int, @IsInitialCaseDraft bit,
            @VerifiedApprovalId int, @VerifiedRepairId int,
            @CurrentApprovalId int, @CurrentApprovalStatus varchar(9), @LatestRepairId int;

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
                   @IsInitialCaseDraft = IsInitialCaseDraft,
                   @VerifiedApprovalId = VerifiedApprovalId, @VerifiedRepairId = VerifiedRepairId
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
                    SELECT TOP (1) @CurrentApprovalId = RepairApprovalId, @CurrentApprovalStatus = ApprovalStatus
                    FROM dbo.RepairApproval
                    WHERE RepairCaseId = @CaseId
                    ORDER BY RequestedAt DESC, RepairApprovalId DESC;
                    IF @CurrentApprovalStatus IS NULL OR @CurrentApprovalStatus <> ''APPROVED''
                        THROW 51016, ''The current repair proposal needs customer approval before reinspection.'', 1;

                    SELECT TOP (1) @LatestRepairId = RepairAttemptId
                    FROM dbo.RepairAttempt
                    WHERE RepairCaseId = @CaseId AND RepairApprovalId = @CurrentApprovalId
                    ORDER BY PerformedAt DESC, RepairAttemptId DESC;
                    IF @LatestRepairId IS NULL
                        THROW 51018, ''Log a repair attempt for the current proposal before reinspection.'', 1;
                    IF @VerifiedApprovalId IS NULL OR @VerifiedApprovalId <> @CurrentApprovalId
                        OR @VerifiedRepairId IS NULL OR @VerifiedRepairId <> @LatestRepairId
                        THROW 51019, ''This draft no longer matches the approved repair. Start a new reinspection; this draft remains saved.'', 1;

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
                    SELECT a.ItemCode
                    FROM @Answers AS a
                    GROUP BY a.ItemCode
                    HAVING COUNT(*) > 1
                )
                THROW 51012, ''Provide valid checklist answers without duplicates.'', 1;

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

            IF @TargetedFailure = 1 AND (
                @CaseId IS NULL OR NOT EXISTS (SELECT 1 FROM @Answers WHERE Outcome = N''FAIL'')
            )
                THROW 51021, ''Record at least one failure with a finding in this repair case.'', 1;

            SET @Outcome = CASE WHEN @TargetedFailure = 1 OR EXISTS (
                SELECT 1 FROM @Answers AS a
                JOIN dbo.ChecklistItem AS ci ON ci.ChecklistVersionId = @VersionId
                                             AND ci.ItemCode = a.ItemCode
                WHERE a.Outcome = N''FAIL'' AND ci.FailInspectionOnFailure = 1
            ) THEN ''FAIL'' ELSE ''PASS'' END;

            -- A blocking failure is conclusive. A pass still requires every checklist item.
            IF @Outcome = ''PASS'' AND EXISTS (
                SELECT 1 FROM dbo.ChecklistItem AS ci
                WHERE ci.ChecklistVersionId = @VersionId
                  AND NOT EXISTS (SELECT 1 FROM @Answers AS a WHERE a.ItemCode = ci.ItemCode)
            )
                THROW 51020, ''Complete all checklist items before recording a pass.'', 1;

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

                INSERT dbo.ReinspectionAttempt
                    (RepairCaseId, VehicleId, AttemptNumber, InspectionId, RepairNote, VerifiedApprovalId, VerifiedRepairId)
                VALUES (@CaseId, @VehicleId, @AttemptNumber, @InspectionId, @RepairNote, @VerifiedApprovalId, @VerifiedRepairId);

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
           s.FinalizedAt, s.FinalizedBy,
           CAST(CASE WHEN i.Outcome = ''PASS'' AND EXISTS (
               SELECT 1 FROM dbo.ChecklistResult AS r
               WHERE r.InspectionId = i.InspectionId AND r.Outcome = ''FAIL''
           ) THEN 1 ELSE 0 END AS bit) AS HasAdvisories
    FROM dbo.InspectionSubmission AS s
    JOIN dbo.Inspection AS i ON i.InspectionId = s.InspectionId
    WHERE s.RequestId = @RequestId;
END;';


