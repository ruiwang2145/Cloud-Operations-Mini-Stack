# =============================================================================
# One-command local setup for Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#
# The Windows counterpart of scripts/bootstrap.sh. Same steps, same idempotence,
# because "works on macOS and Linux" is only half a setup story and the reviewer
# with a Windows laptop is the one who finds out.
#
# Environment switches:
#   -Python <path>       interpreter to build the venv from (default: py -3)
#   -SkipSuperuser       do not prompt for an admin account
#   -SkipInstall         assume dependencies are already installed
# =============================================================================
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$VenvDir = ".venv",
    [switch]$SkipSuperuser,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

function Write-Step  { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Green }
function Write-Info  { param([string]$Text) Write-Host "    $Text" }
function Write-Warn2 { param([string]$Text) Write-Host "  !  $Text" -ForegroundColor Yellow }
function Stop-WithError { param([string]$Text) Write-Host "error: $Text" -ForegroundColor Red; exit 1 }

# --- 1. interpreter ---------------------------------------------------------
Write-Step "Checking the Python interpreter"

if ($Python) {
    $PythonExe = $Python
} else {
    # `py -3` is the launcher that ships with python.org installers and picks the
    # newest Python 3 on PATH. Falling back to `python` covers the Microsoft Store
    # build and conda.
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($launcher) { $PythonExe = "py" } else { $PythonExe = "python" }
}

try {
    $version = & $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])"
} catch {
    Stop-WithError "'$PythonExe' not found. Install Python 3.10+ or pass -Python <path>."
}

Write-Info "using $PythonExe ($version)"

& $PythonExe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Python 3.10 or newer is required (Django 5.2 dropped older versions)."
}

# --- 2. virtualenv ----------------------------------------------------------
Write-Step "Preparing the virtual environment"

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $VenvPython) {
    Write-Info "$VenvDir already exists, reusing it"
} else {
    & $PythonExe -m venv $VenvDir
    Write-Info "created $VenvDir"
}

if (-not (Test-Path $VenvPython)) { Stop-WithError "no interpreter found inside $VenvDir" }

# --- 3. dependencies --------------------------------------------------------
if ($SkipInstall) {
    Write-Step "Skipping dependency installation (-SkipInstall)"
} else {
    Write-Step "Installing dependencies"
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r requirements.txt --quiet
    Write-Info "runtime dependencies installed"
    & $VenvPython -m pip install -r requirements-dev.txt --quiet
    Write-Info "development dependencies installed"
}

# --- 4. environment file ----------------------------------------------------
Write-Step "Preparing .env"

if (Test-Path ".env") {
    Write-Info ".env already exists, leaving it untouched"
    Write-Info "(delete it and re-run to regenerate, or edit it by hand)"
} else {
    $script = @'
import pathlib
import secrets

example = pathlib.Path(".env.example")
target = pathlib.Path(".env")

if not example.exists():
    raise SystemExit("error: .env.example is missing, cannot create .env")

text = example.read_text(encoding="utf-8")
key = secrets.token_urlsafe(50)

if "SECRET_KEY=replace-me" not in text:
    raise SystemExit("error: .env.example no longer contains the SECRET_KEY placeholder")
text = text.replace("SECRET_KEY=replace-me", f"SECRET_KEY={key}", 1)

target.write_text(text, encoding="utf-8")
print("    wrote .env with a generated SECRET_KEY")
'@
    $tmp = Join-Path $env:TEMP "cloudops_env_setup.py"
    # Written with an explicit UTF-8 encoding and no BOM: PowerShell 5.1 defaults
    # to UTF-16 for `Set-Content`, and Python refuses to run a UTF-16 file.
    [System.IO.File]::WriteAllText($tmp, $script, (New-Object System.Text.UTF8Encoding($false)))
    & $VenvPython $tmp
    Remove-Item $tmp -Force
}

# --- 5. database ------------------------------------------------------------
Write-Step "Applying database migrations"
& $VenvPython manage.py migrate --noinput
Write-Info "database schema is up to date"

# --- 6. sanity check --------------------------------------------------------
Write-Step "Running Django's system checks"
& $VenvPython manage.py check

# --- 7. optional admin user -------------------------------------------------
if (-not $SkipSuperuser) {
    Write-Step "Admin account (optional)"
    Write-Info "The admin is only needed to browse the data at /admin/."
    $answer = Read-Host "    Create a superuser now? [y/N]"
    if ($answer -match '^[yY]') {
        & $VenvPython manage.py createsuperuser
    } else {
        Write-Info "skipped; run 'manage.py createsuperuser' later if you want one"
    }
}

# --- 8. next steps ----------------------------------------------------------
Write-Host ""
Write-Host "Ready." -ForegroundColor Green
Write-Host @"

  Start the service
    $VenvPython manage.py runserver

  Then, in another terminal
    $VenvPython scripts/smoke_test.py          # verify every endpoint
    $VenvPython -m pytest                      # run the test suite

  Useful URLs
    http://localhost:8000/healthz/            liveness
    http://localhost:8000/readyz/             readiness (503 when a dependency is down)
    http://localhost:8000/metrics             Prometheus scrape target
    http://localhost:8000/api/tasks/          the REST API
    http://localhost:8000/api/slo/            availability SLO report
    http://localhost:8000/admin/              Django admin

  The whole stack, with Prometheus and Grafana
    docker compose up -d --build
    docker compose --profile demo up -d       # adds a load generator

"@
