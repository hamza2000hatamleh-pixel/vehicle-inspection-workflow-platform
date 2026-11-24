USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID(N'dbo.Vehicle')
        AND name = N'ModelYear' AND is_nullable = 0
)
    ALTER TABLE dbo.Vehicle ALTER COLUMN ModelYear smallint NULL;

COMMIT;


