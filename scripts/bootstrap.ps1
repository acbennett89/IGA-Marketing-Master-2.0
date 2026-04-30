# bootstrap.ps1 -- IGA Marketing Master 2.0 first-time setup (Windows / PowerShell).
#
# Idempotent: safe to re-run. Detects an existing .venv, an installed package,
# and an installed Playwright browser, and skips work it doesn't need to do.
#
# Steps:
#   1. Verify Python 3.13 is available.
#   2. Create .venv if missing (uses py launcher: `py -3.13 -m venv .venv`).
#   3. Activate the venv for this script's session.
#   4. Upgrade pip.
#   5. Install dependencies from requirements.txt.
#   6. Install Chromium for Playwright.
#   7. Editable-install the package (pip install -e .).
#   8. Health check: import iga_marketing_master_2.
#
# Run from a normal (non-elevated) PowerShell prompt. Requires:
#   - Python 3.13 installed (via python.org installer or Microsoft Store).
#   - PowerShell 5.1+ (ships with Windows 10/11).
#   - Network access for pip + Playwright browser download.
#
# Usage (from repo root):
#   .\scripts\bootstrap.ps1
#
# If you see an execution-policy error, run once per PowerShell session:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Write-Info($Message)    { Write-Host "[..] $Message" -ForegroundColor Cyan }
function Write-Ok($Message)      { Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Skip($Message)    { Write-Host "[--] $Message" -ForegroundColor Yellow }
function Write-Fail($Message)    { Write-Host "[!!] $Message" -ForegroundColor Red }

# Resolve repo root from the script location ($PSScriptRoot is scripts/).
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot
Write-Info "Repo root: $RepoRoot"

# 1. Verify Python 3.13. ------------------------------------------------
Write-Info "Checking for Python 3.13..."
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    Write-Fail "The 'py' launcher was not found. Install Python 3.13 from https://www.python.org/downloads/ (with the 'py launcher' option enabled), then re-run this script."
    exit 1
}

try {
    $pyVersionOutput = & py -3.13 --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw "py -3.13 exited with $LASTEXITCODE" }
} catch {
    Write-Fail "Python 3.13 is not installed (or not registered with the py launcher). Install it from https://www.python.org/downloads/ and re-run this script."
    Write-Fail "Detail: $_"
    exit 1
}
Write-Ok "Found $pyVersionOutput"

# 2. Create .venv if missing. -------------------------------------------
$VenvPath  = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
if (Test-Path $VenvPython) {
    Write-Skip ".venv already exists; skipping create."
} else {
    Write-Info "Creating virtual environment at $VenvPath ..."
    & py -3.13 -m venv $VenvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Write-Fail "Failed to create .venv. Exit code: $LASTEXITCODE"
        exit 1
    }
    Write-Ok "Virtual environment created."
}

# Print the venv's Python version so the operator can verify the right
# interpreter is being used.
$venvPyVersion = & $VenvPython --version 2>&1
Write-Info "Venv Python: $venvPyVersion"
if ($venvPyVersion -notmatch '3\.13') {
    Write-Fail "Venv reports '$venvPyVersion' (expected Python 3.13). Delete .venv and re-run."
    exit 1
}

# 3. Upgrade pip. -------------------------------------------------------
Write-Info "Upgrading pip..."
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip upgrade failed."
    exit 1
}
Write-Ok "pip upgraded."

# 4. Install dependencies. ---------------------------------------------
$ReqFile = Join-Path $RepoRoot 'requirements.txt'
if (-not (Test-Path $ReqFile)) {
    Write-Fail "requirements.txt not found at $ReqFile."
    exit 1
}
Write-Info "Installing dependencies from requirements.txt..."
& $VenvPython -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install -r requirements.txt failed."
    exit 1
}
Write-Ok "Dependencies installed."

# 5. Install Playwright Chromium. --------------------------------------
Write-Info "Installing Playwright Chromium (this may take a minute)..."
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Fail "playwright install chromium failed."
    exit 1
}
Write-Ok "Playwright Chromium installed."

# 6. Editable install of this package. ---------------------------------
Write-Info "Installing iga-marketing-master-2 in editable mode..."
& $VenvPython -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install -e . failed."
    exit 1
}
Write-Ok "Package installed (editable)."

# 7. Health check: import the package. ---------------------------------
Write-Info "Health check: importing iga_marketing_master_2..."
& $VenvPython -c "import iga_marketing_master_2; print('iga-marketing-master-2', iga_marketing_master_2.__version__)"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Health check failed -- the package did not import cleanly."
    exit 1
}
Write-Ok "Health check passed."

# 8. Done. -------------------------------------------------------------
Write-Host ""
Write-Ok "Bootstrap complete."
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Activate the venv:    .\.venv\Scripts\Activate.ps1"
Write-Host "  2. Launch the app:       iga-marketing-master-2"
Write-Host "  3. On first run, you'll be prompted for your Anthropic API key."
Write-Host ""

exit 0
