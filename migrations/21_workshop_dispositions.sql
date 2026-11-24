USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection' OR OBJECT_ID(N'dbo.DiagnosticCheck', N'U') IS NULL
    THROW 50000, 'Run 20_diagnostic_work_log.sql first.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.RepairApproval', N'ApprovalStatus') = 8
BEGIN
    ALTER TABLE dbo.RepairApproval DROP CONSTRAINT CK_RepairApproval_Decision, DF_RepairApproval_Status;
    ALTER TABLE dbo.RepairApproval ALTER COLUMN ApprovalStatus varchar(9) NOT NULL;
    ALTER TABLE dbo.RepairApproval ADD CONSTRAINT DF_RepairApproval_Status DEFAULT 'PENDING' FOR ApprovalStatus;
    ALTER TABLE dbo.RepairApproval ADD CONSTRAINT CK_RepairApproval_Decision CHECK (
        (ApprovalStatus = 'PENDING' AND DecidedAt IS NULL AND DecisionRecordedBy IS NULL AND DecisionNote IS NULL)
        OR (ApprovalStatus IN ('APPROVED', 'DECLINED', 'WITHDRAWN')
            AND DecidedAt IS NOT NULL AND DecidedAt >= RequestedAt
            AND DecisionRecordedBy IS NOT NULL AND LEN(TRIM(DecisionRecordedBy)) > 0
            AND (ApprovalStatus <> 'WITHDRAWN' OR (DecisionNote IS NOT NULL AND LEN(TRIM(DecisionNote)) > 0)))
    );
END;

IF COL_LENGTH(N'dbo.RepairCase', N'CloseoutReason') IS NULL
BEGIN
    ALTER TABLE dbo.RepairCase ADD CloseoutReason nvarchar(1000) NULL, ClosedBy nvarchar(100) NULL;
    EXEC sys.sp_executesql N'
        ALTER TABLE dbo.RepairCase DROP CONSTRAINT CK_RepairCase_Closure;
        ALTER TABLE dbo.RepairCase ADD CONSTRAINT CK_RepairCase_Closure CHECK (
            ClosingOutcome = ''PASS'' AND (
                (CloseoutReason IS NULL AND ClosedBy IS NULL AND (
                    (ClosedAt IS NULL AND ClosingInspectionId IS NULL)
                    OR (ClosedAt IS NOT NULL AND InitialInspectionId IS NOT NULL AND (
                        (InitialOutcome = ''PASS'' AND ClosingInspectionId IS NULL)
                        OR (InitialOutcome = ''FAIL'' AND ClosingInspectionId IS NOT NULL)
                    ))
                ))
                OR (CloseoutReason IS NOT NULL AND LEN(TRIM(CloseoutReason)) > 0
                    AND ClosedBy IS NOT NULL AND LEN(TRIM(ClosedBy)) > 0
                    AND ClosedAt IS NOT NULL AND ClosingInspectionId IS NULL
                    AND InitialInspectionId IS NOT NULL AND InitialOutcome = ''FAIL'')
            )
        );
    ';
END;

IF COL_LENGTH(N'dbo.InspectionDraft', N'VerifiedRepairId') IS NULL
BEGIN
    ALTER TABLE dbo.InspectionDraft ADD
        VerifiedApprovalId int NULL REFERENCES dbo.RepairApproval (RepairApprovalId),
        VerifiedRepairId int NULL REFERENCES dbo.RepairAttempt (RepairAttemptId);
END;

IF COL_LENGTH(N'dbo.ReinspectionAttempt', N'VerifiedRepairId') IS NULL
BEGIN
    ALTER TABLE dbo.ReinspectionAttempt ADD
        VerifiedApprovalId int NULL REFERENCES dbo.RepairApproval (RepairApprovalId),
        VerifiedRepairId int NULL REFERENCES dbo.RepairAttempt (RepairAttemptId);
END;

EXEC sys.sp_executesql N'GRANT UPDATE (ClosedAt, CloseoutReason, ClosedBy) ON dbo.RepairCase TO InspectionApp;';

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

COMMIT;


