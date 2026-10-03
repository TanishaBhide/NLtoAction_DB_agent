<#
Live PostgreSQL validation for the NL-to-Action DB agent (Weeks 1-3).

Recreates a throwaway database, loads both schemas and the FULL raw CSV,
runs every query file, runs integrity checks and negative tests, and saves
everything to docs\validation_output.txt.

Connection: standard libpq environment variables (psql and load_data.py both use them):
  PGHOST (default localhost)  PGPORT (default 5432)  PGUSER (default postgres)  PGPASSWORD
Optional:
  CSV_PATH       path to database_raw.csv   (default: <repo>\database_raw.csv)
  VALIDATION_DB  database to (re)create     (default: nl_agent_validation)
  PYTHON         python executable          (default: python)

Usage (PowerShell, from the repo root, with the venv activated):
  $env:PGPASSWORD = "your_password"
  .\scripts\validate_db.ps1

Works on Windows PowerShell 5.1 and PowerShell 7+.
If script execution is blocked:  powershell -ExecutionPolicy Bypass -File .\scripts\validate_db.ps1
#>

$ErrorActionPreference = 'Stop'

$RootDir = Split-Path -Parent $PSScriptRoot
$DbDir = Join-Path $RootDir 'database'
$OutDir = Join-Path $RootDir 'docs'
$OutFile = Join-Path $OutDir 'validation_output.txt'

function Get-EnvOrDefault([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

$CsvPath = Get-EnvOrDefault 'CSV_PATH' (Join-Path $RootDir 'database_raw.csv')
$DbName = Get-EnvOrDefault 'VALIDATION_DB' 'nl_agent_validation'
$Python = Get-EnvOrDefault 'PYTHON' 'python'
$env:PGHOST = Get-EnvOrDefault 'PGHOST' 'localhost'
$env:PGPORT = Get-EnvOrDefault 'PGPORT' '5432'
$env:PGUSER = Get-EnvOrDefault 'PGUSER' 'postgres'
# The loader must use the validation database, not whatever DATABASE_URL points to.
Remove-Item Env:\DATABASE_URL -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Content -Path $OutFile -Value '' -Encoding UTF8

$script:Failures = 0
$PsqlBase = @('-X', '-v', 'ON_ERROR_STOP=1', '--pset=pager=off')

function Write-Raw([string]$Line) {
    Write-Host $Line
    Add-Content -Path $OutFile -Value $Line -Encoding UTF8
}

function Write-Log([string]$Message) {
    Write-Raw ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

function Write-Section([string]$Title) {
    Write-Raw ''
    Write-Raw ('=' * 64)
    Write-Log $Title
    Write-Raw ('=' * 64)
}

# Runs a native command, logs its output, and returns exit code + output.
# ErrorActionPreference is relaxed during the call because Windows PowerShell 5.1
# turns anything a native program writes to stderr into a terminating error.
function Invoke-Native {
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [switch]$Quiet,
        [switch]$AllowFailure
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $Exe @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if (-not $Quiet) {
        foreach ($line in $lines) { Write-Raw $line }
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        if ($Quiet) {
            foreach ($line in ($lines | Select-Object -Last 20)) { Write-Raw $line }
        }
        throw "'$Exe $($Arguments -join ' ')' failed with exit code $code"
    }
    return [pscustomobject]@{ ExitCode = $code; Output = ($lines -join "`n") }
}

function Invoke-Psql([string[]]$Arguments, [switch]$Quiet, [switch]$AllowFailure) {
    return Invoke-Native -Exe 'psql' -Arguments ($PsqlBase + $Arguments) -Quiet:$Quiet -AllowFailure:$AllowFailure
}

function Get-Scalar([string]$Sql) {
    $result = Invoke-Psql -Arguments @('-tAc', $Sql) -Quiet
    return $result.Output.Trim()
}

function Test-Equals([string]$Label, [string]$Sql, [string]$Expected) {
    $actual = Get-Scalar $Sql
    if ($actual -ceq $Expected) {
        Write-Log "PASS: $Label = $actual"
    }
    else {
        Write-Log "FAIL: $Label = '$actual' (expected '$Expected')"
        $script:Failures++
    }
}

# Each statement runs inside BEGIN ... ROLLBACK so an unexpected success changes nothing.
function Test-ExpectError([string]$Label, [string]$ExpectedMessage, [string]$Sql) {
    $result = Invoke-Psql -Arguments @('-c', "BEGIN; $Sql ROLLBACK;") -Quiet -AllowFailure
    $errorLine = ($result.Output -split "`n" | Where-Object { $_ -match 'ERROR' } | Select-Object -First 1)
    if ($result.ExitCode -eq 0) {
        Write-Log "FAIL: $Label - statement was ACCEPTED but must be rejected"
        $script:Failures++
    }
    elseif ($result.Output.Contains($ExpectedMessage)) {
        Write-Log "EXPECTED ERROR ($Label): $errorLine"
    }
    elseif ($result.Output.Contains('permission denied to set role')) {
        Write-Log "SKIPPED ($Label): PGUSER cannot SET ROLE; run as a superuser to test role permissions"
    }
    else {
        Write-Log "FAIL: $Label - rejected with an unexpected error: $errorLine"
        $script:Failures++
    }
}

try {
    Write-Section 'NL-to-Action DB agent - live PostgreSQL validation'
    Write-Log "repo:      $RootDir"
    Write-Log "csv:       $CsvPath"
    Write-Log "server:    $($env:PGUSER)@$($env:PGHOST):$($env:PGPORT)"
    Write-Log "database:  $DbName (dropped and recreated)"

    if (-not (Get-Command 'psql' -ErrorAction SilentlyContinue)) {
        throw "psql not found on PATH. Add PostgreSQL's bin folder, e.g. C:\Program Files\PostgreSQL\16\bin"
    }
    if (-not (Test-Path $CsvPath)) {
        throw "CSV not found at $CsvPath (set `$env:CSV_PATH)"
    }
    Write-Log ("python:    " + (Invoke-Native -Exe $Python -Arguments @('--version') -Quiet).Output)
    Write-Log ("psql:      " + (Invoke-Native -Exe 'psql' -Arguments @('--version') -Quiet).Output)

    Write-Section '1. Recreate database'
    Invoke-Psql -Arguments @('-d', 'postgres', '-c', "DROP DATABASE IF EXISTS $DbName;", '-c', "CREATE DATABASE $DbName;") | Out-Null
    $env:PGDATABASE = $DbName
    Write-Log ("server version: " + (Get-Scalar 'SHOW server_version;'))

    Write-Section '2. Load schemas'
    # Hide the harmless "table does not exist, skipping" NOTICEs from DROP ... IF EXISTS
    # without hiding errors: ON_ERROR_STOP still fails the step on any error.
    $env:PGOPTIONS = '-c client_min_messages=warning'
    foreach ($file in @('schema.sql', 'agent_schema.sql')) {
        Write-Log "running database\$file"
        Invoke-Psql -Arguments @('-q', '-f', (Join-Path $DbDir $file)) | Out-Null
    }
    Remove-Item Env:\PGOPTIONS -ErrorAction SilentlyContinue
    Invoke-Psql -Arguments @('-c', '\dt') | Out-Null
    $tableCount = Get-Scalar "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';"
    Write-Log "public tables: $tableCount (expected 12)"
    if ($tableCount -ne '12') { Write-Log 'FAIL: expected 12 tables'; $script:Failures++ }

    Write-Section '3. Load the full raw dataset'
    $timer = [Diagnostics.Stopwatch]::StartNew()
    Invoke-Native -Exe $Python -Arguments @((Join-Path $DbDir 'load_data.py'), '--csv', $CsvPath, '--reset') | Out-Null
    Write-Log ("loader finished in {0:N0} s" -f $timer.Elapsed.TotalSeconds)

    Write-Section '4. Agent sample data and query files'
    Write-Log 'running database\agent_sample_data.sql'
    Invoke-Psql -Arguments @('-q', '-f', (Join-Path $DbDir 'agent_sample_data.sql')) | Out-Null
    Write-Log 'running database\queries.sql (result rows discarded - some queries return the full 536k-row join)'
    Invoke-Psql -Arguments @('-q', '-f', (Join-Path $DbDir 'queries.sql')) -Quiet | Out-Null
    Write-Log 'queries.sql: all statements executed without error'

    Write-Section '5. Row counts (all 12 tables)'
    $countsSql = "SELECT 'customers' AS table_name, count(*) AS rows FROM customers " +
        "UNION ALL SELECT 'products', count(*) FROM products " +
        "UNION ALL SELECT 'invoices', count(*) FROM invoices " +
        "UNION ALL SELECT 'invoice_items', count(*) FROM invoice_items " +
        "UNION ALL SELECT 'agent_sessions', count(*) FROM agent_sessions " +
        "UNION ALL SELECT 'session_events', count(*) FROM session_events " +
        "UNION ALL SELECT 'policy_rules', count(*) FROM policy_rules " +
        "UNION ALL SELECT 'column_sensitivity', count(*) FROM column_sensitivity " +
        "UNION ALL SELECT 'policy_rule_columns', count(*) FROM policy_rule_columns " +
        "UNION ALL SELECT 'schema_lookup_log', count(*) FROM schema_lookup_log " +
        "UNION ALL SELECT 'execution_audit', count(*) FROM execution_audit " +
        "UNION ALL SELECT 'compensation_ledger', count(*) FROM compensation_ledger;"
    Invoke-Psql -Arguments @('-c', $countsSql) | Out-Null

    Write-Section '6. Data checks'
    Invoke-Psql -Arguments @('-c', 'SELECT status, count(*) AS invoices FROM invoices GROUP BY status ORDER BY status;') | Out-Null
    Invoke-Psql -Arguments @('-c', 'SELECT count(*) AS guest_invoices FROM invoices WHERE customer_id IS NULL;') | Out-Null
    Invoke-Psql -Arguments @('-c', 'SELECT count(*) AS products_without_description FROM products WHERE description IS NULL;') | Out-Null
    Invoke-Psql -Arguments @('-c', "SELECT stock_code, description FROM products WHERE stock_code IN ('21275', '21109') ORDER BY stock_code;") | Out-Null
    Invoke-Psql -Arguments @('-c', "SELECT i.invoice_no, i.status, ii.unit_price FROM invoices i JOIN invoice_items ii USING (invoice_no) WHERE i.status = 'adjustment' ORDER BY i.invoice_no;") | Out-Null

    Test-Equals 'product 21275 description' "SELECT description FROM products WHERE stock_code = '21275';" 'ZINC TOP  2 DOOR WOODEN SHELF'
    Test-Equals 'product 21109 description' "SELECT description FROM products WHERE stock_code = '21109';" 'LARGE CAKE TOWEL CHOCOLATE SPOTS'
    Test-Equals 'orphan invoice_items' 'SELECT count(*) FROM invoice_items ii LEFT JOIN invoices i USING (invoice_no) WHERE i.invoice_no IS NULL;' '0'
    Test-Equals 'negative price outside adjustments' "SELECT count(*) FROM invoice_items ii JOIN invoices i USING (invoice_no) WHERE ii.unit_price < 0 AND i.status <> 'adjustment';" '0'

    Write-Section '7. Negative tests (each statement MUST be rejected)'
    Test-ExpectError 'negative unit_price on a completed invoice (trigger)' `
        'negative unit_price is only allowed for adjustment invoices' `
        "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES ('536365', '85123A', 1, -5.00);"

    # UPDATE, not INSERT: the status trigger derives status from invoice_no on INSERT,
    # so an inserted 'bogus' status would be silently replaced instead of reaching the CHECK.
    Test-ExpectError 'invalid invoice status (CHECK chk_invoices_status)' `
        'violates check constraint "chk_invoices_status"' `
        "UPDATE invoices SET status = 'bogus' WHERE invoice_no = '536365';"

    Test-ExpectError 'zero quantity (CHECK chk_invoice_items_quantity)' `
        'violates check constraint "chk_invoice_items_quantity"' `
        "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES ('536365', '85123A', 0, 1.00);"

    Test-ExpectError 'line item for a non-existent invoice (FK)' `
        'violates foreign key constraint' `
        "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES ('NO_SUCH_INVOICE', '85123A', 1, 1.00);"

    Test-ExpectError 'dbm_writer editing the append-only audit log' `
        'permission denied for table execution_audit' `
        "SET LOCAL ROLE dbm_writer; UPDATE execution_audit SET error_message = 'tampered';"

    Write-Section 'Result'
    if ($script:Failures -eq 0) {
        Write-Log 'VALIDATION PASSED - output saved to docs\validation_output.txt'
        exit 0
    }
    Write-Log "VALIDATION FAILED - $($script:Failures) check(s) failed"
    exit 1
}
catch {
    Write-Log "VALIDATION FAILED - $($_.Exception.Message)"
    exit 1
}