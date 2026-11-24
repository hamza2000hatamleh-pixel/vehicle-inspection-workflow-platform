USE VehicleInspection;
GO
SET XACT_ABORT ON;
BEGIN TRANSACTION;
IF OBJECT_ID(N'dbo.WorkshopTechnician', N'U') IS NULL
    THROW 50000, 'Install migration 28 first.', 1;
IF OBJECT_ID(N'dbo.WorkshopTechnicianShift', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.WorkshopTechnicianShift (
        NameKey nvarchar(100) NOT NULL PRIMARY KEY,
        OnShift bit NOT NULL,
        ChangedBy nvarchar(100) NOT NULL,
        ChangedAt datetime2(3) NOT NULL DEFAULT SYSUTCDATETIME()
    );
END;
GRANT SELECT, INSERT, UPDATE ON dbo.WorkshopTechnicianShift TO InspectionApp;
COMMIT;


