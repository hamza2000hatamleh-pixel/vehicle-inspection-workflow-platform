# Vehicle Inspection & Repair Workflow Platform

A role-based web app for recording vehicle inspections and managing workshop repair cases through customer approval, repair, and quality-control reinspection.

## What it includes

- Service-advisor, technician, supervisor, and quality-control workspaces
- Vehicle registration, VIN lookup, customer intake, and repair-case queues
- Structured checklists, findings, diagnostics, repair proposals, labor/parts, and repair attempts
- Approval and decline tracking, case handoffs, reinspection, and case history
- English and Arabic interface support
- Recall research and customer reports
- SQL Server migrations and optional role-based staff sign-in

The project has completed a user-reported live vehicle test without errors. The user also confirmed that the final report displayed the customer email.

## Technology

- Python, FastAPI, Pydantic, and Uvicorn
- Microsoft SQL Server through `pyodbc`
- HTML, CSS, and JavaScript
- ODBC Driver 17 for SQL Server (or another driver named in the connection string)

## Local setup

Prerequisites: Python 3.10+, Microsoft SQL Server, `sqlcmd`, and a compatible SQL Server ODBC driver.

1. Create a virtual environment and install the Python dependencies:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Review the numbered SQL scripts in `migrations/`, then apply them in numeric order. The first migration creates the demo database `VehicleInspection`; later scripts update its schema. Back up any database before applying schema changes.

   ```powershell
   Get-ChildItem .\migrations\*.sql |
     Sort-Object { [int]$_.BaseName.Substring(0, 2) } |
     ForEach-Object { sqlcmd -S localhost -E -b -i $_.FullName }
   ```

3. Set a SQL Server connection string and start the local app:

   ```powershell
   $env:INSPECTION_DB_CONNECTION = 'DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;DATABASE=VehicleInspection;Trusted_Connection=yes;'
   python start_inspection.py 8001
   ```

4. Open `http://127.0.0.1:8001/` and choose a workshop role.

The connection string is intentionally supplied through an environment variable. Do not commit credentials, customer data, or `workshop_staff.json`.

### Optional staff sign-in

Run `python configure_workshop_staff.py` to create local workshop accounts, then set `INSPECTION_REQUIRE_STAFF_LOGIN=1` before starting the app. The generated `workshop_staff.json` is ignored by Git.

## Vehicle catalog attribution

The catalog is an adapted snapshot of MeterApp Vehicle DB with additional source records. Refer to [`vehicle_catalog_sources.html`](vehicle_catalog_sources.html) for upstream sources and attribution notes. Coverage varies by market and model year; verify vehicle details against registration documents.

## Safety and deployment

This repository is prepared for local evaluation. The app defaults to localhost and uses a SQL Server connection supplied by the operator. Before any network or production deployment, configure authentication, TLS, database permissions, backups, monitoring, and privacy controls for customer records and reports.

## License

No project-wide license is included. The vehicle catalog and its upstream sources have their own attribution or licensing terms; review those before redistributing the data or assigning a license to this repository.
