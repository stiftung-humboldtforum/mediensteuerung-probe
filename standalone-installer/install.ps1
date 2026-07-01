#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Standalone offline installer wrapper for the Humboldt-Probe.

.DESCRIPTION
    Runs inside the self-contained package built by build-standalone-installer.ps1.
    Deploys the probe to a permanent location, hardens the mTLS key, then hands off
    to the probe's own scripts/install-windows.ps1 to install Python + deps + the
    shawl service -- all offline, no winget.

    Deploy is done here (not in install-windows.ps1) so install-windows.ps1 keeps
    its "install from a checkout in place" semantics for developers.

    Launched by install.cmd (which self-elevates). No internet required.
#>
$ErrorActionPreference = 'Stop'
$here    = $PSScriptRoot
$target  = 'C:\HumboldtProbe'
$service = 'HumboldtProbe'

Write-Host "=== Humboldt-Probe standalone installer ===" -ForegroundColor Cyan

# --- optional config.txt: MQTT_HOST / MQTT_PORT (default srv-control-avm:8883) ---
$mqttHost = 'srv-control-avm'
$mqttPort = '8883'
$cfg = Join-Path $here 'config.txt'
if (Test-Path $cfg) {
    foreach ($line in Get-Content $cfg) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        if ($t -match '^(MQTT_HOST|MQTT_PORT)\s*=\s*(.+?)\s*$') {
            if ($Matches[1] -eq 'MQTT_HOST') { $mqttHost = $Matches[2].Trim() } else { $mqttPort = $Matches[2].Trim() }
        }
    }
}
if ($mqttPort -notmatch '^\d+$' -or [int]$mqttPort -lt 1 -or [int]$mqttPort -gt 65535) {
    Write-Host "ERROR: invalid MQTT port '$mqttPort' in config.txt (must be 1-65535)." -ForegroundColor Red
    exit 1
}
Write-Host ("MQTT broker: {0}:{1}" -f $mqttHost, $mqttPort) -ForegroundColor Gray

# --- stop an existing service and WAIT until it is actually stopped/gone, so its
#     python.exe releases the lib DLLs + log before we overwrite them. sc stop is
#     async, so poll sc query rather than sleeping a fixed interval. ---
& sc.exe stop $service 2>&1 | Out-Null
for ($i = 0; $i -lt 30; $i++) {
    $q = & sc.exe query $service 2>&1
    if ($LASTEXITCODE -ne 0 -or ($q -match 'STOPPED')) { break }   # gone, or stopped
    Start-Sleep -Seconds 1
}

# --- deploy payload to the permanent install dir. Use robocopy /MIR: it mirrors
#     the CONTENTS into the target (Copy-Item -Recurse would NEST as $target\src\src
#     on a re-install and the service would keep running STALE code), and /R retries
#     files a slow-stopping probe may still hold briefly. ---
Write-Host "Deploying probe to $target ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force $target | Out-Null
function Deploy-Dir($Src, $Dst) {
    robocopy $Src $Dst /MIR /R:5 /W:2 /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Write-Host "ERROR: deploy $Src -> $Dst failed (robocopy $LASTEXITCODE) -- is the probe still running / a file locked?" -ForegroundColor Red
        exit 1
    }
}
Deploy-Dir (Join-Path $here 'src') (Join-Path $target 'src')
Deploy-Dir (Join-Path $here 'lib') (Join-Path $target 'lib')

# --- certs: deploy to $target\certs\ (same layout as the kiosk). Copy the private
#     key FIRST and harden it IMMEDIATELY, to minimise the window in which the fleet
#     mTLS key sits with inherited (broad) ACLs. SIDs are German-safe: *S-1-5-18 =
#     LocalSystem, *S-1-5-32-544 = Administrators. Fail closed. (install-windows.ps1
#     re-hardens too; both are idempotent.) ---
$certDir = Join-Path $target 'certs'
New-Item -ItemType Directory -Force $certDir | Out-Null
$key = Join-Path $certDir 'client_key.pem'
Copy-Item (Join-Path $here 'certs\client_key.pem') $key -Force
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
try { & icacls $key /inheritance:r /grant:r '*S-1-5-18:R' '*S-1-5-32-544:R' 2>&1 | Out-Null; $ic = $LASTEXITCODE }
finally { $ErrorActionPreference = $prevEAP }
if ($ic -ne 0) {
    Write-Host "ERROR: icacls hardening of client_key.pem failed ($ic) -- refusing to continue." -ForegroundColor Red
    exit 1
}
Write-Host "client_key.pem ACL hardened (LocalSystem + Administrators only)." -ForegroundColor Green

# public certs + config (not secret).
Copy-Item (Join-Path $here 'certs\ca_certificate.pem')     (Join-Path $certDir 'ca_certificate.pem')     -Force
Copy-Item (Join-Path $here 'certs\client_certificate.pem') (Join-Path $certDir 'client_certificate.pem') -Force
if (-not (Test-Path (Join-Path $target 'userconfig.txt'))) {
    Copy-Item (Join-Path $here 'userconfig.example.txt') (Join-Path $target 'userconfig.txt') -Force
}

# --- hand off to the probe's service installer (offline: python + deps + shawl) ---
$svcInstaller = Join-Path $here 'scripts\install-windows.ps1'
if (-not (Test-Path $svcInstaller)) {
    Write-Host "ERROR: $svcInstaller not found -- the package is incomplete." -ForegroundColor Red
    exit 1
}
& $svcInstaller -InstallPath $target -MqttHostname $mqttHost -MqttPort $mqttPort
exit $LASTEXITCODE
