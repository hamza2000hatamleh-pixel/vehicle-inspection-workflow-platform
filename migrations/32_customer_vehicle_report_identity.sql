IF DB_NAME() NOT IN (N'VehicleInspection', N'VehicleInspection')
    THROW 50000, 'Select VehicleInspection or VehicleInspection before running this migration.', 1;
IF OBJECT_ID(N'dbo.RepairCase', N'U') IS NULL
    THROW 50000, 'Apply the vehicle inspection schema migrations through 31 first.', 1;
GO

SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.RepairCase', N'CustomerName') IS NULL
    ALTER TABLE dbo.RepairCase ADD CustomerName nvarchar(120) NULL;

IF COL_LENGTH(N'dbo.RepairCase', N'CustomerPhone') IS NULL
    ALTER TABLE dbo.RepairCase ADD CustomerPhone nvarchar(40) NULL;

IF COL_LENGTH(N'dbo.RepairCase', N'CustomerEmail') IS NULL
    ALTER TABLE dbo.RepairCase ADD CustomerEmail nvarchar(254) NULL;

GRANT UPDATE (CustomerName, CustomerPhone, CustomerEmail)
    ON OBJECT::dbo.RepairCase TO InspectionApp;

COMMIT;
GO

