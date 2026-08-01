<#
    Starts the QA platform from PowerShell, freeing the port first.

    The plain `python -m uvicorn ...` command fails with WinError 10048 whenever a
    previous server (or a stray one left by a test run) is still holding the port,
    and uvicorn reports that *after* printing "Application startup complete",
    which makes it look like the app started when it did not. This script clears
    the port first, so that never happens.

        .\run_server.ps1              # http://127.0.0.1:8000
        .\run_server.ps1 -Port 8080
        .\run_server.ps1 -Reload      # auto-restart when code changes
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Address = "127.0.0.1",
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "======================================================" -ForegroundColor DarkGray
Write-Host " AI QA Automation Platform" -ForegroundColor Cyan
Write-Host " Workspace: $PSScriptRoot" -ForegroundColor DarkGray
Write-Host "======================================================" -ForegroundColor DarkGray

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[ERROR] No virtual environment at .venv" -ForegroundColor Red
    Write-Host "        python -m venv .venv"
    Write-Host "        .venv\Scripts\Activate.ps1"
    Write-Host "        pip install -r requirements.txt"
    Write-Host "        playwright install chromium"
    exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Write-Host "[WARN] No .env file - AI generation will be disabled." -ForegroundColor Yellow
    Write-Host "       Copy .env.example to .env and add your GEMINI_API_KEY."
}

# ── Free the port ────────────────────────────────────────────────────
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    foreach ($owner in ($listeners.OwningProcess | Select-Object -Unique)) {
        $proc = Get-Process -Id $owner -ErrorAction SilentlyContinue
        $label = if ($proc) { "$($proc.ProcessName) (PID $owner, started $($proc.StartTime))" }
                 else { "PID $owner" }
        Write-Host "Port $Port is held by $label - stopping it..." -ForegroundColor Yellow
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "[ERROR] Port $Port is still in use. Another user may own that process;" -ForegroundColor Red
        Write-Host "        run with a different port:  .\run_server.ps1 -Port 8080" -ForegroundColor Red
        exit 1
    }
    Write-Host "Port $Port released." -ForegroundColor Green
}

# ── Start ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Dashboard: http://$Address`:$Port" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

$arguments = @("-m", "uvicorn", "qa_agent:app", "--host", $Address, "--port", "$Port")
if ($Reload) { $arguments += "--reload" }

& $python @arguments
