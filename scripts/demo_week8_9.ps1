# One-command demo for Weeks 8-9 (Windows PowerShell).
#   1. starts (or reuses) the PostgreSQL 16 container on port 55432 (needs Docker Desktop)
#   2. creates .venv and installs requirements if missing
#   3. loads schema + sample data + agent tables (incl. Week 8/9), runs every Week 8/9 scenario,
#      then the Week 9 evaluation
#
#   powershell -ExecutionPolicy Bypass -File scripts\demo_week8_9.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\demo_week8_9.ps1 -Pause
#   powershell -ExecutionPolicy Bypass -File scripts\demo_week8_9.ps1 -Interactive
param(
    [switch]$Pause,
    [switch]$Interactive
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$container = "nl2action-pg"
$exists = docker ps -a --filter "name=^$container$" --format "{{.Names}}"
if (-not $exists) {
    Write-Host "Starting PostgreSQL container $container on port 55432 ..."
    docker run -d --name $container -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=agent `
        -e POSTGRES_DB=online_retail_db -p 55432:5432 postgres:16-alpine | Out-Null
} else {
    docker start $container | Out-Null
}
for ($i = 0; $i -lt 30; $i++) {
    docker exec $container pg_isready -U agent -d online_retail_db *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv and installing requirements ..."
    py -3 -m venv .venv
    .venv\Scripts\python -m pip install -q -r requirements.txt
}

$env:DATABASE_URL = "postgresql://agent:agent@localhost:55432/online_retail_db"
.venv\Scripts\python database\setup_demo_db.py --dataset sample   # the demo scenarios are written for the 13-row sample

if ($Interactive) {
    .venv\Scripts\python database\react_agent.py --interactive
} elseif ($Pause) {
    .venv\Scripts\python database\demo_week8_9.py --no-reset --pause
} else {
    .venv\Scripts\python database\demo_week8_9.py --no-reset
    .venv\Scripts\python database\eval_week9.py
}
