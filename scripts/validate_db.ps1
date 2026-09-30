$ErrorActionPreference = 'Stop'

$rootDir = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $rootDir 'docs'
$outputFile = Join-Path $outputDir 'validation_output.txt'

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

@"
Validation script invoked at $(Get-Date -Format o)
Root: $rootDir
CSV: $(Join-Path $rootDir 'database_raw.csv')
This script is a local validation wrapper for a real PostgreSQL installation.
"@ | Set-Content -Path $outputFile

Write-Host 'Use a local PostgreSQL server and run:'
Write-Host 'psql -U <user> -d <database> -f database/schema.sql'
Write-Host 'psql -U <user> -d <database> -f database/agent_schema.sql'
Write-Host 'python database/load_data.py --reset'
Write-Host 'psql -U <user> -d <database> -f database/agent_sample_data.sql'
Write-Host 'psql -U <user> -d <database> -f database/queries.sql'
Write-Host "Write the resulting terminal output into $outputFile"