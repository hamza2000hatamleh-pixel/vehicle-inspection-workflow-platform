# Power BI reporting contract

Use the curated SQL views created by migration `30_reporting_and_followups.sql`. Map a separate SQL login to the database role `InspectionReportingReader`. The role has `SELECT` permission on the curated views only; do not map Power BI to the application's `InspectionApp` role or grant it direct table access.

## Views

| View | Grain | Use |
| --- | --- | --- |
| `dbo.vw_PBI_RepairCaseSummary` | One row per repair case | Current case stage, owner, stage start, latest approval/repair/reinspection, and test-data classification |
| `dbo.vw_PBI_ApprovalIntervals` | One row per completed approval decision | Request-to-decision duration in seconds |
| `dbo.vw_PBI_InspectionIntervals` | One row per initial inspection or repair-to-reinspection interval | Intake and repair verification durations in seconds |
| `dbo.vw_PBI_QCAttempts` | One row per reinspection attempt | QC outcome and cumulative failed attempts per case |

Every view carries `IsTestData`; each also exposes `IsReportable` where appropriate. Filter `IsReportable = 1` for operational totals. Keep test data visible in a separate QA page when useful.

## Metric rules

- **Current backlog:** repair cases with `ClosedAt IS NULL`; this is a current snapshot and does not use the selected historical window.
- **Intake to inspection:** initial `InspectedAt - OpenedAt`, attributed to the initial inspection timestamp.
- **Approval wait:** `DecidedAt - RequestedAt` for approved or declined proposals only, attributed to `DecidedAt`. Pending proposals are tracked as current work, not completed intervals.
- **Repair to reinspection:** the reinspection timestamp minus the applicable repair `PerformedAt`. Prefer the repair linked by `VerifiedRepairId`; when that relationship is absent, use the latest eligible repair recorded before the inspection and after the prior inspection.
- **Repeat QC failure:** count failed reinspection attempts per case. The existing supervisor report flags cases with at least two failed attempts.
- **Time zone:** stored workflow timestamps are UTC. Convert for display in the report layer; do not change the source timestamps.
- **Time window:** for completed interval measures, filter by the event timestamp named above. Do not filter the current open backlog by that historical window.

## Test-data classification

`RepairCase.IsTestData` is the reporting source of truth. Migration 30 backfills known historical demo, QA, pilot, and smoke records once; future classification is an explicit supervisor action in the case details. Do not infer test status from fleet-code prefixes in Power BI measures.

## Reconciliation before publishing

For each view, choose representative case IDs, compare the row values with the case timeline and source records, and record the case IDs used. Verify both an `IsTestData = 1` case and an `IsTestData = 0` case. Confirm the reporting database role can select the views and cannot select or modify application tables.
