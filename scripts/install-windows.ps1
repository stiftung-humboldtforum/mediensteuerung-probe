#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs the Humboldt-Probe as a Windows service via NSSM.

.DESCRIPTION
    Idempotent install / re-install script. Sets up an NSSM service
    that runs src/app.py with the configured MQTT broker, and pipes
    stdout/stderr into a rotating log file.

    Re-running the script with different arguments updates the existing
    service in place (stop → reconfigure → start).

.PARAMETER InstallPath
    Where the source tree lives. Default: C:\humboldt-probe

.PARAMETER ConfigFile
    Path to userconfig.txt. Default: <InstallPath>\userconfig.txt

.PARAMETER MqttHostname
    MQTT broker hostname. Required.

.PARAMETER MqttPort
    MQTT port. Default: 8883 (1883 wenn -NoTls).

.PARAMETER CaCertificate, CertFile, KeyFile
    TLS material. Required wenn nicht -NoTls.

.PARAMETER NoTls
    Schaltet TLS ab. Nur fuer lokales Testing — siehe Banner-Warnung
    der App.

.PARAMETER LogLevel
    Default: INFO

.EXAMPLE
    .\install-windows.ps1 -MqttHostname mqtt.example.com `
        -CaCertificate C:\hp\ca.pem -CertFile C:\hp\client.pem `
        -KeyFile C:\hp\client.key

.EXAMPLE
    .\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls
#>
param(
    [string]$InstallPath  = "C:\humboldt-probe",
    [string]$ConfigFile   = "",
    [Parameter(Mandatory)] [string]$MqttHostname,
    [int]   $MqttPort     = 0,
    [string]$CaCertificate = "",
    [string]$CertFile     = "",
    [string]$KeyFile      = "",
    [switch]$NoTls,
    [ValidateSet('CRITICAL','ERROR','WARNING','INFO','DEBUG')]
    [string]$LogLevel     = "INFO",
    [string]$ServiceName  = "HumboldtProbe",
    [string]$PythonExe    = "C:\Program Files\Python313\python.exe"
)

$ErrorActionPreference = 'Stop'

function Assert-Command($cmd) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "Required command '$cmd' not found on PATH."
    }
}

# --- Pre-flight checks ----------------------------------------------------
Assert-Command nssm

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found at $PythonExe — install via 'winget install Python.Python.3.13 --scope machine' or pass -PythonExe."
}

if (-not (Test-Path $InstallPath)) {
    throw "Install path '$InstallPath' does not exist. Copy the source tree there first."
}

if (-not $ConfigFile) {
    $ConfigFile = Join-Path $InstallPath "userconfig.txt"
}
if (-not (Test-Path $ConfigFile)) {
    throw "Config file '$ConfigFile' not found."
}

if (-not $NoTls) {
    foreach ($f in @($CaCertificate, $CertFile, $KeyFile)) {
        if (-not $f -or -not (Test-Path $f)) {
            throw "TLS material missing or not found: '$f'. Pass -CaCertificate / -CertFile / -KeyFile, or use -NoTls."
        }
    }
}

if ($MqttPort -eq 0) {
    $MqttPort = if ($NoTls) { 1883 } else { 8883 }
}

# --- Build app.py argument string -----------------------------------------
$srcPath = Join-Path $InstallPath "src"
$logPath = Join-Path $InstallPath "probe.log"

$appArgs = @(
    "app.py",
    "--config_file=$ConfigFile",
    "--mqtt_hostname=$MqttHostname",
    "--mqtt_port=$MqttPort",
    "--loglevel=$LogLevel"
)
if ($NoTls) {
    $appArgs += "--no_tls"
} else {
    $appArgs += "--ca_certificate=$CaCertificate"
    $appArgs += "--certfile=$CertFile"
    $appArgs += "--keyfile=$KeyFile"
}
$appArgsString = $appArgs -join ' '

# --- Install / re-install service -----------------------------------------
$existing = nssm status $ServiceName 2>$null
if ($existing) {
    Write-Host "Service '$ServiceName' exists — stopping for reconfigure..."
    nssm stop $ServiceName confirm | Out-Null
} else {
    Write-Host "Installing service '$ServiceName'..."
    nssm install $ServiceName $PythonExe $appArgsString
}

# Reconfigure (idempotent)
nssm set $ServiceName Application $PythonExe                    | Out-Null
nssm set $ServiceName AppParameters $appArgsString              | Out-Null
nssm set $ServiceName AppDirectory $srcPath                     | Out-Null
nssm set $ServiceName AppStdout $logPath                        | Out-Null
nssm set $ServiceName AppStderr $logPath                        | Out-Null
nssm set $ServiceName AppRotateFiles 1                          | Out-Null
nssm set $ServiceName AppRotateBytes 1048576                    | Out-Null
nssm set $ServiceName Start SERVICE_AUTO_START                  | Out-Null
nssm set $ServiceName AppExit Default Restart                   | Out-Null
nssm set $ServiceName AppRestartDelay 5000                      | Out-Null

Write-Host "Starting '$ServiceName'..."
nssm start $ServiceName | Out-Null

Write-Host ""
Write-Host "Done. Status:"
nssm status $ServiceName

Write-Host ""
Write-Host "Log: $logPath"
Write-Host "Manage: nssm {start|stop|restart|status} $ServiceName"
