USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection' OR COL_LENGTH(N'dbo.RepairCase', N'OdometerKm') IS NULL
    THROW 50000, 'Run 18_case_intake_details.sql first.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.RepairCase', N'OdometerMiles') IS NULL
    ALTER TABLE dbo.RepairCase ADD OdometerMiles int NULL
        CONSTRAINT CK_RepairCase_OdometerMiles CHECK (OdometerMiles BETWEEN 0 AND 9999999);

EXEC sys.sp_executesql N'
    IF OBJECT_ID(N''dbo.CK_RepairCase_OdometerUnit'', N''C'') IS NULL
        ALTER TABLE dbo.RepairCase ADD CONSTRAINT CK_RepairCase_OdometerUnit
            CHECK (OdometerKm IS NULL OR OdometerMiles IS NULL);
    GRANT UPDATE (OdometerMiles) ON OBJECT::dbo.RepairCase TO InspectionApp;
';

COMMIT;


