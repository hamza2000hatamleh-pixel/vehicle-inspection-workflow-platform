USE VehicleInspection;
GO
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF DB_NAME() <> N'VehicleInspection' OR OBJECT_ID(N'dbo.FinalizeInspection', N'P') IS NULL
    THROW 52000, 'Run 02_harden_inspection_workflow.sql first.', 1;

DECLARE @VehicleId int, @VersionId int, @DemoCaseId int;
SELECT @VehicleId = i.VehicleId, @VersionId = i.ChecklistVersionId, @DemoCaseId = rc.RepairCaseId
FROM dbo.Inspection AS i
JOIN dbo.RepairCase AS rc ON rc.InitialInspectionId = i.InspectionId
WHERE i.InspectionCode = 'DEMO-INITIAL-001';

IF @DemoCaseId IS NULL
    THROW 52001, 'The original demo case is missing.', 1;

BEGIN TRY
    BEGIN TRANSACTION;
    DECLARE @DraftId uniqueidentifier = NEWID(), @RequestId uniqueidentifier = NEWID(),
            @Revision binary(8), @CaseId int, @InspectionId int;
    DECLARE @FailPayload nvarchar(max) = N'{
        "answers":[{"itemCode":"BRAKE_LIGHT","outcome":"FAIL"},{"itemCode":"HEADLIGHT","outcome":"PASS"}],
        "defects":[{"itemCode":"BRAKE_LIGHT","severity":"MAJOR","finding":"Brake light still intermittent."}],
        "repairNote":"Checked the bulb and retested."
    }';
    DECLARE @PassPayload nvarchar(max) = N'{
        "answers":[{"itemCode":"BRAKE_LIGHT","outcome":"PASS"},{"itemCode":"HEADLIGHT","outcome":"PASS"}],
        "defects":[],
        "repairNote":"Replaced the faulty connector and retested."
    }';

    INSERT dbo.InspectionDraft (DraftId, VehicleId, ChecklistVersionId, Payload)
    VALUES (@DraftId, @VehicleId, @VersionId, @FailPayload);
    SELECT @Revision = Revision FROM dbo.InspectionDraft WHERE DraftId = @DraftId;
    EXEC dbo.FinalizeInspection @DraftId, @Revision, @RequestId;
    EXEC dbo.FinalizeInspection @DraftId, @Revision, @RequestId;

    SELECT @CaseId = RepairCaseId, @InspectionId = InspectionId
    FROM dbo.InspectionSubmission WHERE RequestId = @RequestId;
    IF @CaseId IS NULL OR @CaseId = @DemoCaseId OR NOT EXISTS (
        SELECT 1 FROM dbo.RepairCase
        WHERE RepairCaseId = @CaseId AND InitialInspectionId = @InspectionId AND ClosedAt IS NULL
    )
        THROW 52002, 'The failed inspection did not create one new open case.', 1;

    SET @DraftId = NEWID();
    SET @RequestId = NEWID();
    INSERT dbo.InspectionDraft (DraftId, VehicleId, ChecklistVersionId, RepairCaseId, Payload)
    VALUES (@DraftId, @VehicleId, @VersionId, @CaseId, @FailPayload);
    SELECT @Revision = Revision FROM dbo.InspectionDraft WHERE DraftId = @DraftId;
    EXEC dbo.FinalizeInspection @DraftId, @Revision, @RequestId;

    IF NOT EXISTS (
        SELECT 1 FROM dbo.RepairCase AS rc
        JOIN dbo.ReinspectionAttempt AS ra ON ra.RepairCaseId = rc.RepairCaseId
        JOIN dbo.Inspection AS i ON i.InspectionId = ra.InspectionId
        WHERE rc.RepairCaseId = @CaseId AND rc.ClosedAt IS NULL
          AND ra.AttemptNumber = 1 AND i.Outcome = 'FAIL'
    )
        THROW 52003, 'The failed reinspection did not leave the case open.', 1;

    SET @DraftId = NEWID();
    SET @RequestId = NEWID();
    INSERT dbo.InspectionDraft (DraftId, VehicleId, ChecklistVersionId, RepairCaseId, Payload)
    VALUES (@DraftId, @VehicleId, @VersionId, @CaseId, @PassPayload);
    SELECT @Revision = Revision FROM dbo.InspectionDraft WHERE DraftId = @DraftId;
    EXEC dbo.FinalizeInspection @DraftId, @Revision, @RequestId;

    IF NOT EXISTS (
        SELECT 1 FROM dbo.RepairCase AS rc
        JOIN dbo.ReinspectionAttempt AS ra ON ra.RepairCaseId = rc.RepairCaseId
                                          AND ra.InspectionId = rc.ClosingInspectionId
        JOIN dbo.Inspection AS i ON i.InspectionId = ra.InspectionId
        WHERE rc.RepairCaseId = @CaseId AND rc.ClosedAt IS NOT NULL
          AND ra.AttemptNumber = 2 AND i.Outcome = 'PASS'
    )
        THROW 52004, 'The passing reinspection did not close the case.', 1;

    ROLLBACK;
    SELECT 'PASS' AS VerificationResult,
           'Retry, failed reinspection, and passing reinspection verified. Test records rolled back.' AS Details;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK;
    THROW;
END CATCH;


