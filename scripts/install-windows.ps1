#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs the Humboldt-Probe as a Windows service via shawl.

.DESCRIPTION
    Idempotent install / re-install script. Creates a Windows service
    (via shawl) that runs src/app.py with the configured MQTT broker;
    shawl captures the wrapped command's stdout/stderr into a rotating
    log file.

    Re-running the script tears down the existing service and recreates
    it (stop -> delete -> recreate -> start) -- shawl bakes its config
    into the service binPath at creation time, so reconfiguration means
    recreation, not in-place edits.

.PARAMETER InstallPath
    Where the source tree lives. Default: C:\humboldt-probe

.PARAMETER ConfigFile
    Path to userconfig.txt. Default: <InstallPath>\userconfig.txt

.PARAMETER MqttHostname
    MQTT broker hostname. Default: srv-control-avm.

.PARAMETER MqttPort
    MQTT port. Default: 8883 (1883 wenn -NoTls).

.PARAMETER CaCertificate, CertFile, KeyFile
    TLS material. Default: ca_certificate.pem / client_certificate.pem /
    client_key.pem im -InstallPath. Override per Param.

.PARAMETER NoTls
    Schaltet TLS ab. Nur fuer lokales Testing -- siehe Banner-Warnung
    der App.

.PARAMETER LogLevel
    Default: INFO

.PARAMETER ServiceUser
    Optional service account (e.g. "NT AUTHORITY\NetworkService"), applied
    via sc.exe after creation. Default: empty (LocalSystem). Use a
    low-privilege account only where LibreHardwareMonitor admin rights are
    not required (a non-admin account yields empty sensor readings).

.PARAMETER ServicePassword
    SecureString. Required for non-system service accounts that need a
    password. Pass via `Read-Host -AsSecureString` or `ConvertTo-SecureString`.

.PARAMETER ShawlExe
    Path to shawl.exe (service wrapper). Default: "shawl" (expected on PATH,
    e.g. via `winget install mtkennerly.shawl`, scoop, or a bundled copy).

.EXAMPLE
    .\install-windows.ps1
    # Default: -MqttHostname srv-control-avm + Certs aus C:\humboldt-probe\

.EXAMPLE
    .\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls
#>
param(
    [string]$InstallPath  = "C:\humboldt-probe",
    [string]$ConfigFile   = "",
    [string]$MqttHostname = "srv-control-avm",
    [int]   $MqttPort     = 0,
    [string]$CaCertificate = "",
    [string]$CertFile     = "",
    [string]$KeyFile      = "",
    [switch]$NoTls,
    [ValidateSet('CRITICAL','ERROR','WARNING','INFO','DEBUG')]
    [string]$LogLevel     = "INFO",
    [string]$ServiceName  = "HumboldtProbe",
    [string]$ServiceUser  = "",
    [securestring]$ServicePassword,
    [string]$PythonExe    = "C:\Program Files\Python313\python.exe",
    [string]$ShawlExe     = "shawl"
)

$ErrorActionPreference = 'Stop'

function Assert-Command($cmd) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "Required command '$cmd' not found on PATH."
    }
}

# --- Pre-flight checks ----------------------------------------------------
Assert-Command $ShawlExe

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found at $PythonExe -- install via 'winget install Python.Python.3.13 --scope machine' or pass -PythonExe."
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
    if (-not $CaCertificate) { $CaCertificate = Join-Path $InstallPath "ca_certificate.pem" }
    if (-not $CertFile)      { $CertFile      = Join-Path $InstallPath "client_certificate.pem" }
    if (-not $KeyFile)       { $KeyFile       = Join-Path $InstallPath "client_key.pem" }
    foreach ($f in @($CaCertificate, $CertFile, $KeyFile)) {
        if (-not (Test-Path $f)) {
            throw "TLS material missing or not found: '$f'. Pass -CaCertificate / -CertFile / -KeyFile, or use -NoTls."
        }
    }
}

if ($MqttPort -eq 0) {
    $MqttPort = if ($NoTls) { 1883 } else { 8883 }
}

# --- Build app.py argument string -----------------------------------------
$srcPath = Join-Path $InstallPath "src"
$logPath = Join-Path $InstallPath "probe_rCURRENT.log"

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
# --- Install / re-install service -----------------------------------------
# shawl bakes its configuration into the service binPath at creation time, so
# "reconfigure" means delete + recreate, not in-place edits. Probe for an
# existing service and tear it down first.
#
# Under Windows PowerShell 5.1 with $ErrorActionPreference='Stop', a native
# command writing to stderr is promoted to a terminating error. sc.exe query
# writes to stderr when the service is absent, so probe under SilentlyContinue
# and key off $LASTEXITCODE, then restore the previous preference.
$serviceExists = $false
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
try {
    & sc.exe query $ServiceName 2>$null | Out-Null
    $serviceExists = ($LASTEXITCODE -eq 0)
} finally {
    $ErrorActionPreference = $prevEAP
}
if ($serviceExists) {
    Write-Host "Service '$ServiceName' exists -- stopping + deleting for recreate..."
    # shawl has NO stop/remove subcommand -- use native sc.exe. A failed stop on
    # an already-stopped service is harmless; swallow stderr to keep re-run
    # idempotency under PS 5.1's stderr-promotes-to-terminating-error behaviour.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        & sc.exe stop $ServiceName 2>$null | Out-Null
        & sc.exe delete $ServiceName 2>$null | Out-Null
        # sc delete only MARKS for deletion -- wait until the service object is
        # gone so the recreate below does not hit error 1072 ("marked for deletion").
        for ($i = 0; $i -lt 15; $i++) {
            & sc.exe query $ServiceName 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { break }
            Start-Sleep -Seconds 1
        }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

# --- Create the service via shawl -----------------------------------------
# Build the whole argument vector as an array -- including a LITERAL '--'
# element -- and splat it, so the '--' separator reaches shawl.exe verbatim (a
# bare `--` token on the command line would be consumed by PowerShell's parser).
#   --cwd       == working directory (app.py is relative; resolves against it).
#   --log-dir/--log-as == combined stdout+stderr capture at
#                  $InstallPath\probe_rCURRENT.log (shawl always appends _rCURRENT).
#   --log-rotate bytes=1048576 == rotate at 1 MB.
#   --restart --restart-delay 5000 == always restart on exit, 5 s throttle.
# No account flag => sc create default = LocalSystem (needed for
# LibreHardwareMonitor sensor access). -ServiceUser overrides it below.
$shawlArgs = @(
    'add',
    '--name',          $ServiceName,
    '--cwd',           $srcPath,
    '--log-dir',       $InstallPath,
    '--log-as',        'probe',
    '--log-rotate',    'bytes=1048576',
    '--log-retain',    '1',
    '--restart',
    '--restart-delay', '5000',
    '--',
    $PythonExe
) + $appArgs

Write-Host "Installing service '$ServiceName'..."
# shawl writes a success line to stderr; under PS 5.1 + EAP=Stop that would
# terminate. Probe under SilentlyContinue and key off $LASTEXITCODE instead.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
try {
    & $ShawlExe @shawlArgs 2>&1 | Out-Null
    $shawlExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEAP
}
if ($shawlExit -ne 0) { throw "shawl add failed (exit $shawlExit)." }

# Description + auto-start are NOT shawl flags. `sc create` (which shawl uses)
# defaults the start type to DEMAND, so `start= auto` is mandatory for the
# service to come up at boot (note the required space after each 'key=').
& sc.exe config $ServiceName start= auto | Out-Null
if ($LASTEXITCODE -ne 0) { throw "sc config start= auto failed (exit $LASTEXITCODE)." }
& sc.exe description $ServiceName "Humboldt-Probe MQTT monitoring agent" | Out-Null

if ($ServiceUser) {
    # A non-LocalSystem account is set the standard Windows way (shawl has no
    # account flag). NOTE: LibreHardwareMonitor needs admin -- a low-privilege
    # account yields empty temperature/fan readings (see README).
    if ($ServicePassword) {
        # Decrypt only at the point of use and zero the buffer right after. The
        # password is briefly on the sc.exe command line -- but no longer held
        # in plaintext script-side, and SecureString keeps it out of history.
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ServicePassword)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            & sc.exe config $ServiceName obj= "$ServiceUser" password= "$plain" | Out-Null
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            Remove-Variable plain -ErrorAction SilentlyContinue
        }
    } else {
        & sc.exe config $ServiceName obj= "$ServiceUser" | Out-Null
    }
    Write-Host "Service account: $ServiceUser"
}

Write-Host "Starting '$ServiceName'..."
& sc.exe start $ServiceName | Out-Null

Write-Host ""
Write-Host "Done. Status:"
& sc.exe query $ServiceName

Write-Host ""
Write-Host "Log: $logPath"
Write-Host "Manage: Start-Service / Stop-Service / Restart-Service / Get-Service $ServiceName"
