#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs the Humboldt-Probe as a Windows service via shawl -- fully offline.

.DESCRIPTION
    Idempotent install / re-install script. Bootstraps the runtime from a local
    offline bundle (no winget / no internet required) and creates a Windows
    service (via shawl) that runs src/app.py with the configured MQTT broker;
    shawl captures the wrapped command's stdout/stderr into a rotating log file.

    OFFLINE BUNDLE: run scripts/prepare-offline.ps1 ONCE on a machine with
    internet to populate installers/ (shawl.exe, the Python installer, the pip
    wheels). After that this script installs everything WITHOUT internet:
      - shawl   from installers/shawl.exe  (copied to C:\Program Files\shawl)
      - Python  from installers/python-3.13.x-amd64.exe (if not already present)
      - deps    from installers/wheels/    (pip --no-index against requirements.lock.txt)
    Each step falls back to an already-present tool (shawl on PATH, an existing
    Python, pre-installed deps), so a connected dev box still works as before.

    Re-running the script tears down the existing service and recreates it
    (stop -> delete -> recreate -> start) -- shawl bakes its config into the
    service binPath at creation time, so reconfiguration means recreation.

.PARAMETER InstallPath
    Where the probe is installed. Default: C:\HumboldtProbe (same as the kiosk).

.PARAMETER ConfigFile
    Path to userconfig.txt. Default: <InstallPath>\userconfig.txt

.PARAMETER MqttHostname
    MQTT broker hostname. Default: srv-control-avm.

.PARAMETER MqttPort
    MQTT port. Default: 8883 (1883 wenn -NoTls).

.PARAMETER CaCertificate, CertFile, KeyFile
    TLS material. Default: <InstallPath>\certs\{ca_certificate,client_certificate,
    client_key}.pem (same layout as the kiosk). Override per Param.

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

.PARAMETER InstallersDir
    Offline bundle directory (shawl.exe, python-*.exe, wheels/). Default:
    <repo>\installers, populated by scripts/prepare-offline.ps1.

.PARAMETER PythonExe
    Python executable. Default C:\Program Files\Python313\python.exe;
    auto-installed offline from installers\python-3.13.x-amd64.exe if missing.

.PARAMETER ShawlExe
    shawl.exe path. Default: shawl on PATH if present, otherwise the bundled
    installers\shawl.exe (installed to C:\Program Files\shawl).

.PARAMETER SkipDeps
    Skip the offline pip install (use when the dependencies are already
    installed, e.g. an editable dev checkout).

.EXAMPLE
    .\install-windows.ps1
    # Default: -MqttHostname srv-control-avm + Certs aus C:\HumboldtProbe\certs\

.EXAMPLE
    .\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls
#>
param(
    [string]$InstallPath  = "C:\HumboldtProbe",
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
    [string]$InstallersDir = "",
    [string]$PythonExe    = "C:\Program Files\Python313\python.exe",
    [string]$ShawlExe     = "shawl",
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'

# --- Resolve repo + offline-bundle paths ----------------------------------
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallersDir) { $InstallersDir = Join-Path $repoRoot 'installers' }
$wheelsDir    = Join-Path $InstallersDir 'wheels'
$requirements = Join-Path $repoRoot 'requirements.lock.txt'

# --- Step 1: shawl (service wrapper) --------------------------------------
# Prefer shawl on PATH; otherwise install the bundled copy to a stable
# location (the service binPath must point at a persistent shawl.exe, not the
# repo checkout). No winget / no download needed when the bundle is present.
Write-Host "Step 1: Ensuring shawl (service wrapper)..."
$shawlCmd = Get-Command $ShawlExe -ErrorAction SilentlyContinue
if ($shawlCmd) {
    $ShawlExe = $shawlCmd.Source
    Write-Host "  -> using shawl on PATH: $ShawlExe"
} else {
    $bundledShawl = Join-Path $InstallersDir 'shawl.exe'
    if (-not (Test-Path $bundledShawl)) {
        throw "shawl not found on PATH and no bundled '$bundledShawl'. Run scripts\prepare-offline.ps1 (online, once) or pass -ShawlExe."
    }
    $shawlTarget = "C:\Program Files\shawl\shawl.exe"
    New-Item -ItemType Directory -Force (Split-Path -Parent $shawlTarget) | Out-Null
    Copy-Item $bundledShawl $shawlTarget -Force
    $ShawlExe = $shawlTarget
    Write-Host "  -> installed bundled shawl.exe to $ShawlExe"
}

# --- Step 2: Python 3.13 --------------------------------------------------
# Use an existing Python; otherwise install the bundled offline installer.
Write-Host "Step 2: Ensuring Python 3.13..."
if (-not (Test-Path $PythonExe)) {
    $pyInstaller = Get-ChildItem $InstallersDir -Filter 'python-3.13.*-amd64.exe' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $pyInstaller) {
        throw "Python not found at '$PythonExe' and no bundled 'installers\python-3.13.*-amd64.exe'. Run scripts\prepare-offline.ps1 or pass -PythonExe."
    }
    Write-Host "  -> installing $($pyInstaller.Name)..."
    # InstallAllUsers=1 -> C:\Program Files (matches $PythonExe); PrependPath=1
    # puts python on the system PATH; skip launcher/tests; keep pip.
    $pyArgs = '/quiet','InstallAllUsers=1','PrependPath=1','Include_launcher=0','Include_test=0','Include_pip=1','AssociateFiles=0'
    $proc = Start-Process -FilePath $pyInstaller.FullName -ArgumentList $pyArgs -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) { throw "Python installer exited $($proc.ExitCode)." }
    # Refresh PATH so a freshly installed pip/python resolves in this session.
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
}
if (-not (Test-Path $PythonExe)) { throw "Python still not found at '$PythonExe' after install." }
Write-Host "  -> Python: $PythonExe"

# --- Step 3: Python dependencies (offline) --------------------------------
# Install from the bundled wheels with --no-index so a missing wheel is a hard
# error instead of a silent PyPI fallback (the offline target cannot reach it).
Write-Host "Step 3: Installing Python dependencies (offline)..."
if ($SkipDeps) {
    Write-Host "  -> -SkipDeps: assuming dependencies already installed."
} elseif (Test-Path $wheelsDir) {
    if (-not (Test-Path $requirements)) { throw "requirements.lock.txt not found at '$requirements'." }
    & $PythonExe -m pip install --no-index --find-links $wheelsDir -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "offline pip install exited $LASTEXITCODE (wheel missing? re-run prepare-offline.ps1)." }
    Write-Host "  -> dependencies installed offline from $wheelsDir"
} else {
    Write-Host "  -> no bundled wheels at '$wheelsDir'; assuming dependencies already installed."
    Write-Host "     For a fully offline install run scripts\prepare-offline.ps1 first, or pass -SkipDeps to silence this."
}

# --- Pre-flight: runtime config -------------------------------------------
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
    if (-not $CaCertificate) { $CaCertificate = Join-Path $InstallPath "certs\ca_certificate.pem" }
    if (-not $CertFile)      { $CertFile      = Join-Path $InstallPath "certs\client_certificate.pem" }
    if (-not $KeyFile)       { $KeyFile       = Join-Path $InstallPath "certs\client_key.pem" }
    foreach ($f in @($CaCertificate, $CertFile, $KeyFile)) {
        if (-not (Test-Path $f)) {
            throw "TLS material missing or not found: '$f'. Pass -CaCertificate / -CertFile / -KeyFile, or use -NoTls."
        }
    }
    # Harden the fleet mTLS private key: only LocalSystem + Administrators may read
    # it (a leak is fleet-wide). Well-known SIDs are German-safe: *S-1-5-18 =
    # LocalSystem, *S-1-5-32-544 = Administrators. Fail closed; guarded for PS 5.1.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try { & icacls $KeyFile /inheritance:r /grant:r '*S-1-5-18:R' '*S-1-5-32-544:R' 2>&1 | Out-Null; $ic = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prevEAP }
    if ($ic -ne 0) { throw "icacls hardening of $KeyFile failed ($ic) -- refusing to continue with the fleet mTLS key readable." }
    Write-Host "  -> client_key.pem ACL hardened (LocalSystem + Administrators only)."
}

if ($MqttPort -eq 0) {
    $MqttPort = if ($NoTls) { 1883 } else { 8883 }
}

# --- Build app.py argument array ------------------------------------------
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

# --- Step 4: Install / re-install the service -----------------------------
# shawl bakes its configuration into the service binPath at creation time, so
# "reconfigure" means delete + recreate, not in-place edits. Probe for an
# existing service and tear it down first.
#
# Under Windows PowerShell 5.1 with $ErrorActionPreference='Stop', a native
# command writing to stderr is promoted to a terminating error. sc.exe query
# writes to stderr when the service is absent, so probe under SilentlyContinue
# and key off $LASTEXITCODE, then restore the previous preference.
Write-Host "Step 4: Registering service '$ServiceName'..."
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
    Write-Host "  -> exists; stopping + deleting for recreate..."
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
