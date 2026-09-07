<#
  scripts/setup.ps1 - Windows one-shot setup (create venv + run bootstrap)

  Run (execution-policy bypass is the standard form):
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

  Options:
    -CoreOnly      # core only, skip agent-browser (standard install is full)
    -SkipBrowser   # skip browser install (env that already has Chromium)
    -Force         # reinstall even if already present
    -VerbosePip    # stream verbose pip logs (helps when pip looks stuck)

  Python selection: we do NOT trust 'Get-Command py' alone. The 'py' launcher can
  exist but fail (e.g. 'py -3' -> "No installed Python found!"). So each candidate
  (py -3, python, python3) is verified by actually running a 3.10+ check, and the
  first that succeeds is used. If 'py -3' fails, it falls back to 'python'.

  npm / agent-browser are invoked from bootstrap.py as npm.cmd / agent-browser.cmd
  to avoid PowerShell .ps1 execution-policy errors.

  NOTE: messages here are ASCII on purpose. Windows PowerShell 5.1 reads a BOM-less
  UTF-8 .ps1 as ANSI(cp949) and would garble non-ASCII text.
#>
param(
  [switch]$CoreOnly,
  [switch]$SkipBrowser,
  [switch]$Force,
  [switch]$VerbosePip
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot   # parent of scripts/ = repo root
Set-Location $repo

Write-Host "=== web-crawler setup (Windows) ===" -ForegroundColor Cyan

# 1) pick a Python that actually RUNS (3.10+), not just one that exists on PATH.
$check = 'import sys; print(sys.executable); assert sys.version_info >= (3, 10)'
$pyExe = $null
$pyArgs = @()
$savedEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"   # native non-zero exit must not abort probing
foreach ($cand in @("py|-3", "python", "python3")) {
  $parts = $cand -split '\|'
  $exe = $parts[0]
  $rest = @(); if ($parts.Count -gt 1) { $rest = $parts[1..($parts.Count - 1)] }
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
  $global:LASTEXITCODE = 0
  & $exe @rest -c $check 1>$null 2>$null
  if ($LASTEXITCODE -eq 0) { $pyExe = $exe; $pyArgs = $rest; break }
  Write-Host ("[*] '{0} {1}' not usable (Python 3.10+ check failed) - trying next" -f $exe, ($rest -join ' ')) -ForegroundColor DarkGray
}
$ErrorActionPreference = $savedEAP

if (-not $pyExe) {
  Write-Host "[FAIL] No working Python 3.10+ found (tried: py -3, python, python3)." -ForegroundColor Red
  Write-Host "       Check:   python --version    /    py -3 --version" -ForegroundColor Red
  Write-Host "       Install Python 3.10+ from https://www.python.org/downloads/ and re-run." -ForegroundColor Red
  exit 2
}
Write-Host ("[*] Using Python: {0} {1}" -f $pyExe, ($pyArgs -join ' ')) -ForegroundColor DarkGray

# 2) venv (create if missing - first time only) using the verified interpreter
if (-not (Test-Path ".venv")) {
  Write-Host "[*] creating .venv (first time, a few seconds)..." -ForegroundColor Yellow
  & $pyExe @pyArgs -m venv .venv
} else {
  Write-Host "[SKIP] .venv already exists" -ForegroundColor DarkGray
}
$venvPy = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host "[FAIL] venv python missing: $venvPy" -ForegroundColor Red
  Write-Host "       Try manually:  $pyExe $($pyArgs -join ' ') -m venv .venv" -ForegroundColor Red
  exit 2
}

# 3) run bootstrap (deps + browser + agent-browser + preflight)
$bootArgs = @("scripts\bootstrap.py")
if ($CoreOnly)    { $bootArgs += "--core-only" }
if ($SkipBrowser) { $bootArgs += "--skip-browser" }
if ($Force)       { $bootArgs += "--force" }
if ($VerbosePip)  { $bootArgs += "--verbose-pip" }

Write-Host "[*] bootstrap: $venvPy $($bootArgs -join ' ')" -ForegroundColor Cyan
& $venvPy @bootArgs
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
  Write-Host "[OK] setup complete. Activate venv in a new PowerShell:  .\.venv\Scripts\Activate.ps1" -ForegroundColor Green
} elseif ($code -eq 1) {
  Write-Host "[INCOMPLETE] core ready, agent-browser unfinished -> install not complete. See 'next commands' above." -ForegroundColor Yellow
} else {
  Write-Host "[FAIL] core setup incomplete (exit $code). Run the 'next commands' shown above." -ForegroundColor Red
}
exit $code
