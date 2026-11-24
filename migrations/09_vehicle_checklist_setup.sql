USE VehicleInspection;
GO
SET XACT_ABORT ON;
BEGIN TRANSACTION;
GRANT INSERT ON dbo.Vehicle TO InspectionApp;
GRANT INSERT ON dbo.ChecklistVersion TO InspectionApp;
GRANT INSERT ON dbo.ChecklistItem TO InspectionApp;
COMMIT;


