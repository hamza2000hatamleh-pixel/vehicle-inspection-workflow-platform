USE VehicleInspection;
GO

CREATE OR ALTER VIEW dbo.vw_ResumableInspectionDraft
AS
SELECT d.DraftId, d.VehicleId, v.FleetCode, d.ChecklistVersionId, cv.VersionCode,
       d.RepairCaseId, d.CreatedAt, d.Revision, d.Payload
FROM dbo.InspectionDraft AS d
JOIN dbo.Vehicle AS v ON v.VehicleId = d.VehicleId
JOIN dbo.ChecklistVersion AS cv ON cv.ChecklistVersionId = d.ChecklistVersionId
LEFT JOIN dbo.RepairCase AS rc ON rc.RepairCaseId = d.RepairCaseId
WHERE rc.ClosedAt IS NULL
  AND NOT EXISTS (SELECT 1 FROM dbo.InspectionSubmission AS s WHERE s.DraftId = d.DraftId);
GO

GRANT SELECT ON dbo.vw_ResumableInspectionDraft TO InspectionApp;
GO


