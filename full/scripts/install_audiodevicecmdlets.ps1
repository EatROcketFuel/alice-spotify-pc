# Optional: PowerShell backend for audio switching ("audio": {"backend": "powershell"}).
# The default pycaw backend does not need this module.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not (Get-PackageProvider -ListAvailable -Name NuGet -ErrorAction SilentlyContinue)) {
    Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Scope CurrentUser -Force | Out-Null
}
Install-Module -Name AudioDeviceCmdlets -Scope CurrentUser -Force
Import-Module AudioDeviceCmdlets
Get-AudioDevice -List | Where-Object { $_.Type -eq 'Playback' } | Format-Table Index, Default, Name -AutoSize
