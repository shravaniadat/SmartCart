# SmartMart demo on Windows (no hardware). Usage from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\run_demo.ps1            (port 5000)
#   powershell -ExecutionPolicy Bypass -File .\run_demo.ps1 -Port 5001
param([int]$Port = 5000)
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$py = '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host 'Creating virtual environment (.venv) ...'
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { py -3 -m venv .venv } else { python -m venv .venv }
    if (-not (Test-Path $py)) { throw 'Python 3.9+ is required: https://www.python.org/downloads/' }
}
& $py -c "import flask, qrcode" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing requirements ...'
    & $py -m pip install --disable-pip-version-check -q -r requirements.txt
}
& $py app.py --demo --port $Port
