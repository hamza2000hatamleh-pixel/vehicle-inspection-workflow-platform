USE VehicleInspection;
GO
IF DB_NAME() <> N'VehicleInspection' OR COL_LENGTH(N'dbo.Vehicle', N'VIN') IS NULL
    THROW 50000, 'Run 13_vehicle_vin_registry.sql first.', 1;

GRANT UPDATE (FleetCode, Make, Model, ModelYear, Powertrain, VIN)
    ON OBJECT::dbo.Vehicle TO InspectionApp;


