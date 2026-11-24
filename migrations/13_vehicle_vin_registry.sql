USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection'
    THROW 50000, 'Run this script in VehicleInspection.', 1;

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH(N'dbo.Vehicle', N'VIN') IS NULL
    ALTER TABLE dbo.Vehicle ADD VIN varchar(17) NULL
        CONSTRAINT CK_Vehicle_VIN CHECK (
            DATALENGTH(VIN) = 17 AND VIN COLLATE Latin1_General_100_BIN2 NOT LIKE '%[^A-HJ-NPR-Z0-9]%'
        );
IF COL_LENGTH(N'dbo.Vehicle', N'EngineDescription') IS NULL
    ALTER TABLE dbo.Vehicle ADD EngineDescription nvarchar(120) NULL;
IF COL_LENGTH(N'dbo.Vehicle', N'TransmissionDescription') IS NULL
    ALTER TABLE dbo.Vehicle ADD TransmissionDescription nvarchar(120) NULL;

EXEC sys.sp_executesql N'
    IF NOT EXISTS (SELECT 1 FROM sys.indexes
        WHERE object_id = OBJECT_ID(N''dbo.Vehicle'') AND name = N''UX_Vehicle_VIN'')
        CREATE UNIQUE INDEX UX_Vehicle_VIN ON dbo.Vehicle (VIN) WHERE VIN IS NOT NULL;

    GRANT UPDATE (VIN, EngineDescription, TransmissionDescription)
        ON OBJECT::dbo.Vehicle TO InspectionApp;
';

COMMIT;


