#requires -Version 5.1
<#
.SYNOPSIS
    Build a self-contained OFFLINE installer package for the Humboldt-Probe that
    runs on any already-running Windows machine (no re-image, no internet).

.DESCRIPTION
    Assembles a package from this probe repo plus the offline bundle produced by
    scripts/prepare-offline.ps1, plus the TLS material supplied via -TlsDir:

        HumboldtProbe-Setup/
          install.cmd                 <- double-click (self-elevates)
          install.ps1                 <- deploy + key-harden, then hand off
          config.txt                  <- optional MQTT broker override
          README.txt
          src/  lib/  requirements.lock.txt  userconfig.example.txt
          certs/  ca_certificate.pem client_certificate.pem client_key.pem
          installers/  python-*.exe shawl.exe wheels/     (probe deps ONLY)
          scripts/install-windows.ps1                     (verbatim copy)

    install.ps1 deploys the payload to C:\humboldt-probe, hardens the key, then
    calls scripts/install-windows.ps1 (the probe's own service installer) which
    installs Python + deps + the shawl service offline. install-windows.ps1 is
    reused verbatim -- no duplicated service logic.

    The TLS certs are NOT in this repo (deployment secrets). Pass them in with
    -TlsDir; the resulting package contains the fleet mTLS key -> treat as secret.

.PARAMETER TlsDir
    Directory containing ca_certificate.pem, client_certificate.pem,
    client_key.pem. Required.

.PARAMETER OutDir
    Output directory. Default: standalone-installer/dist (gitignored).

.PARAMETER Zip
    Also produce HumboldtProbe-Setup.zip.

.EXAMPLE
    .\standalone-installer\build-standalone-installer.ps1 -TlsDir ..\windows11-boot\tls -Zip
#>
param(
    [Parameter(Mandatory = $true)][string]$TlsDir,
    [string]$OutDir = (Join-Path $PSScriptRoot 'dist'),
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'
$repoRoot   = Split-Path -Parent $PSScriptRoot
$instDir    = Join-Path $repoRoot 'installers'
$svcScript  = Join-Path $repoRoot 'scripts\install-windows.ps1'

function Need($Path, $Hint) {
    if (-not (Test-Path $Path)) {
        Write-Host "ERROR: missing $Path" -f Red
        if ($Hint) { Write-Host "  $Hint" -f Yellow }
        exit 1
    }
}

Write-Host ""
Write-Host "=== build-standalone-installer (probe) ===" -f Cyan
Write-Host ""

# -----------------------------------------------------------------------------
# Validate inputs.
# -----------------------------------------------------------------------------
Need "$repoRoot\src\app.py"             "Run from a full probe checkout."
Need "$repoRoot\requirements.lock.txt"
Need "$repoRoot\userconfig.example.txt"
if (-not (Test-Path "$repoRoot\lib" -PathType Container)) { Write-Host "ERROR: missing $repoRoot\lib" -f Red; exit 1 }
Need $svcScript "Probe checkout incomplete."
foreach ($c in 'ca_certificate.pem', 'client_certificate.pem', 'client_key.pem') {
    Need "$TlsDir\$c" "Pass -TlsDir <dir with ca/client cert + key> (from the vault / windows11-boot\tls)."
}
$py = Get-ChildItem $instDir -Filter 'python-3.13.*-amd64.exe' -ErrorAction SilentlyContinue |
    Sort-Object { [version]($_.BaseName -replace 'python-', '' -replace '-amd64', '') } -Descending |
    Select-Object -First 1
if (-not $py) { Write-Host "ERROR: no python-3.13.x-amd64.exe in $instDir -- run scripts\prepare-offline.ps1." -f Red; exit 1 }
Need "$instDir\shawl.exe" "Run scripts\prepare-offline.ps1."
$wheels = Get-ChildItem "$instDir\wheels" -Filter '*.whl' -ErrorAction SilentlyContinue
if (-not $wheels) { Write-Host "ERROR: no wheels in $instDir\wheels -- run scripts\prepare-offline.ps1." -f Red; exit 1 }

# Wheels must satisfy the CURRENT lock (same gate build_stick uses): a stale set
# would pass a count check yet fail pip --no-index on the target.
$lockHash = (Get-FileHash "$repoRoot\requirements.lock.txt" -Algorithm SHA256).Hash
$manifest = "$instDir\bundle.manifest.json"
$mItems = $null
if (Test-Path $manifest) { try { $mItems = (Get-Content $manifest -Raw | ConvertFrom-Json).items } catch {} }
$mWheels = if ($mItems) { $mItems.wheels } else { $null }
if (-not $mWheels -or -not $mWheels.lockHash) {
    Write-Host "ERROR: bundle.manifest.json has no wheels.lockHash -- run scripts\prepare-offline.ps1." -f Red; exit 1
}
if ($mWheels.lockHash -ne $lockHash) {
    Write-Host "ERROR: bundled wheels are stale vs requirements.lock.txt -- re-run scripts\prepare-offline.ps1." -f Red; exit 1
}
if ($mWheels.count -and ($wheels.Count -ne $mWheels.count)) {
    Write-Host "ERROR: wheel count mismatch (disk $($wheels.Count), manifest $($mWheels.count)) -- re-run prepare-offline.ps1." -f Red; exit 1
}
# Integrity: python.exe + shawl.exe on disk must match the manifest SHA256 that
# prepare-offline recorded -- catches a corrupted or swapped bundled binary.
foreach ($chk in @(@{ f = $py.FullName; m = $mItems.python }, @{ f = "$instDir\shawl.exe"; m = $mItems.shawl })) {
    if ($chk.m -and $chk.m.sha256) {
        if ((Get-FileHash $chk.f -Algorithm SHA256).Hash -ne $chk.m.sha256) {
            Write-Host "ERROR: $(Split-Path $chk.f -Leaf) SHA256 mismatch vs manifest -- re-run scripts\prepare-offline.ps1." -f Red; exit 1
        }
    }
}

Write-Host "Inputs OK:" -f Green
Write-Host "  Python:  $($py.Name)" -f Gray
Write-Host "  shawl:   shawl.exe" -f Gray
Write-Host "  Wheels:  $($wheels.Count) packages" -f Gray
Write-Host "  TLS:     $TlsDir" -f Gray
Write-Host ""

# -----------------------------------------------------------------------------
# Assemble.
# -----------------------------------------------------------------------------
$pkg = Join-Path $OutDir 'HumboldtProbe-Setup'
if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pkg, "$pkg\certs", "$pkg\installers", "$pkg\scripts" | Out-Null

function Invoke-Robocopy($Src, $Dst, [string[]]$Extra) {
    $rcArgs = @($Src, $Dst) + $Extra + @('/NFL', '/NDL', '/NJH', '/NJS', '/R:1', '/W:1')
    robocopy @rcArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Host "ERROR: robocopy $Src -> $Dst failed (exit $LASTEXITCODE)." -f Red; exit 1 }
}

Invoke-Robocopy "$repoRoot\src" "$pkg\src" @('/E', '/XD', '__pycache__', '.pytest_cache', '/XF', '*.pyc')
Invoke-Robocopy "$repoRoot\lib" "$pkg\lib" @('/E')
Copy-Item "$repoRoot\requirements.lock.txt"  "$pkg\requirements.lock.txt"  -Force
Copy-Item "$repoRoot\userconfig.example.txt" "$pkg\userconfig.example.txt" -Force

foreach ($c in 'ca_certificate.pem', 'client_certificate.pem', 'client_key.pem') {
    Copy-Item "$TlsDir\$c" "$pkg\certs\$c" -Force
}

# installers: probe deps ONLY (python + shawl + wheels) -- NOT mosquitto / Git.
Copy-Item $py.FullName        "$pkg\installers\" -Force
Copy-Item "$instDir\shawl.exe" "$pkg\installers\" -Force
Invoke-Robocopy "$instDir\wheels" "$pkg\installers\wheels" @('/E')

# the probe's service installer -- verbatim (single source of truth).
Copy-Item $svcScript "$pkg\scripts\install-windows.ps1" -Force

# launcher + wrapper + config + readme.
Copy-Item "$PSScriptRoot\install.cmd"        "$pkg\install.cmd"  -Force
Copy-Item "$PSScriptRoot\install.ps1"        "$pkg\install.ps1"  -Force
Copy-Item "$PSScriptRoot\config.txt.example" "$pkg\config.txt"   -Force
Copy-Item "$PSScriptRoot\PACKAGE-README.txt" "$pkg\README.txt"   -Force

# -----------------------------------------------------------------------------
# Post-check: everything install.ps1 / install-windows.ps1 need.
# -----------------------------------------------------------------------------
$must = @(
    "$pkg\install.cmd", "$pkg\install.ps1", "$pkg\config.txt", "$pkg\README.txt",
    "$pkg\src\app.py", "$pkg\lib", "$pkg\requirements.lock.txt", "$pkg\userconfig.example.txt",
    "$pkg\certs\client_key.pem", "$pkg\certs\ca_certificate.pem", "$pkg\certs\client_certificate.pem",
    "$pkg\installers\shawl.exe", "$pkg\installers\wheels",
    "$pkg\scripts\install-windows.ps1"
)
foreach ($m in $must) { if (-not (Test-Path $m)) { Write-Host "ERROR: post-check failed, missing: $m" -f Red; exit 1 } }
if (-not (Get-ChildItem "$pkg\installers" -Filter 'python-3.13.*-amd64.exe')) {
    Write-Host "ERROR: post-check failed, no python installer in package." -f Red; exit 1
}

$sizeMB = [math]::Round((Get-ChildItem $pkg -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Package built: $pkg  ($sizeMB MB)" -f Green

if ($Zip) {
    $zipPath = Join-Path $OutDir 'HumboldtProbe-Setup.zip'
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path "$pkg\*" -DestinationPath $zipPath
    Write-Host "Zip: $zipPath  ($([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB)" -f Green
}

Write-Host ""
Write-Host "Copy the folder (or zip) to the target machine, then run install.cmd as admin." -f Green
