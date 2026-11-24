USE VehicleInspection;
GO

SET NOCOUNT ON;
SET XACT_ABORT ON;

IF DB_NAME() <> N'VehicleInspection' OR COL_LENGTH(N'dbo.RepairCase', N'CloseoutReason') IS NULL
    THROW 50000, 'Apply this migration to VehicleInspection after migration 21.', 1;

BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.RepairCase', N'CancellationReason') IS NULL
BEGIN
    ALTER TABLE dbo.RepairCase ADD CancellationReason nvarchar(1000) NULL, CancelledBy nvarchar(100) NULL;

    EXEC sys.sp_executesql N'
        ALTER TABLE dbo.RepairCase DROP CONSTRAINT CK_RepairCase_Closure;
        ALTER TABLE dbo.RepairCase ADD CONSTRAINT CK_RepairCase_Closure CHECK (
            ClosingOutcome = ''PASS'' AND (
                (CancellationReason IS NULL AND CancelledBy IS NULL AND (
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
                ))
                OR (CancellationReason IS NOT NULL AND LEN(TRIM(CancellationReason)) > 0
                    AND CancelledBy IS NOT NULL AND LEN(TRIM(CancelledBy)) > 0
                    AND ClosedAt IS NOT NULL AND ClosingInspectionId IS NULL
                    AND CloseoutReason IS NULL AND ClosedBy IS NULL)
            )
        );
        GRANT UPDATE (CancellationReason, CancelledBy) ON dbo.RepairCase TO InspectionApp;
    ';
END;

COMMIT;


