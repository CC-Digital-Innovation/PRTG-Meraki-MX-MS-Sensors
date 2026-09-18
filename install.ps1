#Requires -Version 5.1
<#
  Installer for the Meraki MX/MS PRTG sensors.

  On the probe host, from an elevated PowerShell:
    irm https://raw.githubusercontent.com/CC-Digital-Innovation/PRTG-Meraki-MX-MS-Sensors/main/install.ps1 | iex

  Downloads prtg_out.py and the four sensor scripts from GitHub into the
  probe's Script v2 directory (Custom Sensors\scripts).

  Environment overrides:
    PRTG_CUSTOM_SENSORS  Custom Sensors directory (skips auto-detection).
    PRTG_SENSOR_REPO     owner/name of the source repo (default below).
    PRTG_SENSOR_BRANCH   branch or tag to install from (default main).
#>
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$repo   = if ($env:PRTG_SENSOR_REPO)   { $env:PRTG_SENSOR_REPO }   else { 'CC-Digital-Innovation/PRTG-Meraki-MX-MS-Sensors' }
$branch = if ($env:PRTG_SENSOR_BRANCH) { $env:PRTG_SENSOR_BRANCH } else { 'main' }
$files  = @('prtg_out.py', 'meraki_wan_status.py', 'meraki_wan_traffic.py', 'meraki_port_status.py',
            'meraki_device_utilization.py')

if ($env:PRTG_CUSTOM_SENSORS) {
    $root = $env:PRTG_CUSTOM_SENSORS
} else {
    $candidates = @()
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'PRTG Network Monitor\Custom Sensors') }
    if ($env:ProgramFiles)        { $candidates += (Join-Path $env:ProgramFiles 'PRTG Network Monitor\Custom Sensors') }
    $root = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $root) {
        throw "PRTG 'Custom Sensors' directory not found (checked: $($candidates -join '; ')). Set `$env:PRTG_CUSTOM_SENSORS and re-run."
    }
}

$dest = Join-Path $root 'scripts'
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }

$base = "https://raw.githubusercontent.com/$repo/$branch"
foreach ($f in $files) {
    Invoke-WebRequest "$base/$f" -OutFile (Join-Path $dest $f) -UseBasicParsing
    Write-Host "  $f"
}
Write-Host "Installed to $dest" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. In PRTG, add a 'Script v2' sensor and pick the script."
Write-Host "  2. Set the Parameters field (see the README)."
Write-Host "  3. Set the Meraki API key under the device or group 'Credentials for"
Write-Host "     Script Sensors' -> Placeholder 1, untick that section's inherit box,"
Write-Host "     and reference it in the Parameters field as %scriptplaceholder1."
