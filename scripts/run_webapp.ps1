# Start the NL-to-Action web app (Windows PowerShell).
#   1. starts (or reuses) the PostgreSQL 16 container nl2action-pg on port 55432 (needs Docker Desktop)
#   2. creates .venv and installs requirements if missing
#   3. loads schema + sample data + agent tables the first time (or with -Reset)
#   4. starts the web server and opens http://127.0.0.1:8000
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_webapp.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run_webapp.ps1 -Reset        # back to the sample data
#   powershell -ExecutionPolicy Bypass -File scripts\run_webapp.ps1 -Port 8080
param(
    [switch]$Reset,
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$container = "nl2action-pg"
$created = $false
$exists = docker ps -a --filter "name=^$container$" --format "{{.Names}}"
if (-not $exists) {
    Write-Host "Starting PostgreSQL container $container on port 55432 ..."
    docker run -d --name $container -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=agent `
        -e POSTGRES_DB=online_retail_db -p 55432:5432 postgres:16-alpine | Out-Null
    $created = $true
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
if ($Reset -or $created) {
    .venv\Scripts\python database\setup_demo_db.py
}

Start-Job -ScriptBlock { param($p) Start-Sleep -Seconds 3; Start-Process "http://127.0.0.1:$p" } -ArgumentList $Port | Out-Null
.venv\Scripts\python webapp\server.py --port $Port
