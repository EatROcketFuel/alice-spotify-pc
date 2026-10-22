$ErrorActionPreference = 'Stop'
$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Alice PC Controller.lnk'
if (Test-Path $shortcut) {
    Remove-Item $shortcut
    Write-Host "Removed: $shortcut" -ForegroundColor Green
} else {
    Write-Host "Shortcut not found: $shortcut"
}
