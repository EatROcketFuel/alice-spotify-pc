# Creates a shortcut in the current user's Startup folder that starts
# main.py with pythonw.exe (no console window) at Windows logon.
$ErrorActionPreference = 'Stop'

$appDir   = Split-Path -Parent $PSScriptRoot
$pythonw  = Join-Path $appDir '.venv\Scripts\pythonw.exe'
$mainPy   = Join-Path $appDir 'main.py'
$startup  = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startup 'Alice PC Controller.lnk'

if (-not (Test-Path $pythonw)) {
    Write-Host "[ERROR] $pythonw not found. Run install.bat first." -ForegroundColor Red
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($shortcut)
$lnk.TargetPath       = $pythonw
$lnk.Arguments        = '"' + $mainPy + '"'
$lnk.WorkingDirectory = $appDir
$lnk.WindowStyle      = 7
$lnk.Description      = 'Alice PC Controller (MQTT -> Spotify / audio)'
$lnk.Save()

Write-Host "Startup shortcut created:" -ForegroundColor Green
Write-Host "  $shortcut"
Write-Host "Open the folder with Win+R -> shell:startup"
