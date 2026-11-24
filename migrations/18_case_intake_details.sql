USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection' OR OBJECT_ID(N'dbo.RepairCase', N'U') IS NULL
    THROW 50000, 'Run the case workflow migrations in VehicleInspection first.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.RepairCase', N'CustomerConcern') IS NULL
    ALTER TABLE dbo.RepairCase ADD CustomerConcern nvarchar(1000) NULL;

IF COL_LENGTH(N'dbo.RepairCase', N'OdometerKm') IS NULL
    ALTER TABLE dbo.RepairCase ADD OdometerKm int NULL
        CONSTRAINT CK_RepairCase_OdometerKm CHECK (OdometerKm BETWEEN 0 AND 9999999);

EXEC sys.sp_executesql N'
    GRANT UPDATE (CustomerConcern, OdometerKm) ON OBJECT::dbo.RepairCase TO InspectionApp;
';

COMMIT;


