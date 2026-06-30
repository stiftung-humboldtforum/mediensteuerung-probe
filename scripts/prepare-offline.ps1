<#
.SYNOPSIS
    Fetch the complete OFFLINE install bundle for the Humboldt-Probe on Windows.
    Run this ONCE on a machine with internet; afterwards install-windows.ps1
    (and the test setup) work with NO internet and NO winget.

.DESCRIPTION
    Downloads every piece of software the probe install references into
    installers/ (gitignored), so the actual install is fully offline:

      installers/shawl.exe                     service wrapper (pinned + SHA256)
      installers/python-3.13.x-amd64.exe       Python runtime (newest 3.13)
      installers/wheels/                        pip wheels, pinned to requirements.lock.txt
      installers/mosquitto-*-install-windows-x64.exe   test broker (newest)
      installers/Git-*-64-bit.exe              Git for Windows (latest, + SHA256)

    Resolved versions + SHA256 land in installers/bundle.manifest.json.
    Network-tolerant: if a source is unreachable but the file is already
    present, the existing one is kept (so a re-run offline still validates).

.PARAMETER Force
    Re-download every component even if the current version is already present.

.PARAMETER Offline
    Skip all network access; only validate that the bundle is present.

.EXAMPLE
    .\scripts\prepare-offline.ps1
#>
param(
    [switch]$Force,
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # massively speeds up Invoke-WebRequest
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$repoRoot = Split-Path -Parent $PSScriptRoot
$inst     = Join-Path $repoRoot 'installers'
$wheels   = Join-Path $inst 'wheels'
$lock     = Join-Path $repoRoot 'requirements.lock.txt'
$manifestPath = Join-Path $inst 'bundle.manifest.json'

New-Item -ItemType Directory -Force -Path $inst, $wheels | Out-Null

Write-Host ""
Write-Host "=== prepare-offline: fetching the offline install bundle into $inst ===" -f Cyan
if ($Offline) { Write-Host "    (offline mode -- validating existing files only)" -f Yellow }
Write-Host ""

$script:errors   = @()
$script:resolved = @{}
$manifestOld = if (Test-Path $manifestPath) { try { Get-Content $manifestPath -Raw | ConvertFrom-Json } catch { $null } } else { $null }
$manifest    = [ordered]@{ generated = (Get-Date).ToString('o'); host = $env:COMPUTERNAME; items = [ordered]@{} }

# --- helpers --------------------------------------------------------------
function Resolve-OrKeep {
    param([string]$Name, [scriptblock]$Existing, [scriptblock]$Fetch)
    Write-Host "-- $Name" -f Cyan
    $have = & $Existing
    if ($Offline) {
        if ($have) { Write-Host "   offline: keeping $($have.Name)" -f Yellow; Write-Host ""; return $have }
        $script:errors += "${Name}: offline and nothing bundled"
        Write-Host "   ERROR: offline and no existing file" -f Red; Write-Host ""; return $null
    }
    try {
        $f = & $Fetch
        Write-Host ""
        return $f
    } catch {
        if ($have) {
            Write-Host "   WARN: fetch failed ($($_.Exception.Message)); keeping existing $($have.Name)" -f Yellow
            Write-Host ""; return $have
        }
        $script:errors += "${Name}: $($_.Exception.Message)"
        Write-Host "   ERROR: $($_.Exception.Message)" -f Red
        Write-Host ""; return $null
    }
}

function Record {
    param([string]$Key, $File)
    if (-not $File) { return }
    $v = if ($script:resolved.ContainsKey($Key)) { $script:resolved[$Key] }
         elseif ($manifestOld -and $manifestOld.items.$Key) { $manifestOld.items.$Key.version }
         else { 'unknown' }
    $manifest.items[$Key] = [ordered]@{
        version = "$v"; file = $File.Name; bytes = $File.Length
        sha256  = (Get-FileHash $File.FullName -Algorithm SHA256).Hash
    }
}

# Download to a .part file, verify byte count, then atomically move into place.
function Save-Url {
    param([string]$Url, [string]$Dest, [int]$TimeoutSec = 600, [long]$MinBytes = 0)
    $part = "${Dest}.part"
    if (Test-Path $part) { Remove-Item $part -Force }
    $expected = $null
    try {
        $h = (Invoke-WebRequest $Url -Method Head -UseBasicParsing -TimeoutSec 30).Headers['Content-Length']
        if ($h) { $expected = [long]($h | Select-Object -First 1) }
    } catch { }
    Invoke-WebRequest $Url -OutFile $part -UseBasicParsing -TimeoutSec $TimeoutSec
    $got = (Get-Item $part).Length
    if ($expected -and $got -ne $expected) {
        Remove-Item $part -Force
        throw "size mismatch for $Url (got $got, expected $expected) -- truncated download"
    }
    if ($MinBytes -and $got -lt $MinBytes) {
        Remove-Item $part -Force
        throw "downloaded file from $Url is implausibly small ($got bytes < $MinBytes)"
    }
    Move-Item -Force $part $Dest
}

# -----------------------------------------------------------------------------
# shawl -- PINNED + SHA256-verified (matches windows11-boot/prepare_bundle.ps1).
# Bump = change $shawlVer + $shawlSha (SHA256 of shawl-<ver>-win64.zip).
# -----------------------------------------------------------------------------
$shawlVer = 'v1.9.0'
$shawlSha = 'F883C5D09C9BEAE2EFAEABD8513E7D3F57CD1D0864CEC3DF4F4A7B6EE904351C'
$f = Resolve-OrKeep 'shawl (pinned, hash-verified)' `
    -Existing { $p = Join-Path $inst 'shawl.exe'; if (Test-Path $p) { Get-Item $p } } `
    -Fetch {
        $script:resolved['shawl'] = $shawlVer
        $t = Join-Path $inst 'shawl.exe'
        if ((Test-Path $t) -and -not $Force) { Write-Host "   current: shawl.exe ($shawlVer)" -f Green; return (Get-Item $t) }
        Write-Host "   downloading shawl-$shawlVer-win64.zip..." -f Gray
        $zip = Join-Path $env:TEMP "shawl-$shawlVer-win64.zip"
        $ex  = Join-Path $env:TEMP "shawl-$shawlVer-extract"
        Save-Url "https://github.com/mtkennerly/shawl/releases/download/$shawlVer/shawl-$shawlVer-win64.zip" $zip -TimeoutSec 120 -MinBytes 500KB
        $got = (Get-FileHash $zip -Algorithm SHA256).Hash
        if ($got -ne $shawlSha) { Remove-Item $zip -Force; throw "shawl $shawlVer SHA256 mismatch (got $got)" }
        if (Test-Path $ex) { Remove-Item $ex -Recurse -Force }
        Expand-Archive $zip -DestinationPath $ex -Force
        $exe = Get-ChildItem $ex -Filter 'shawl.exe' -Recurse | Select-Object -First 1
        if (-not $exe) { Remove-Item $zip, $ex -Recurse -Force; throw "shawl.exe not found in zip" }
        Copy-Item $exe.FullName $t -Force
        Remove-Item $zip, $ex -Recurse -Force
        Write-Host "   done (SHA256 verified)." -f Green
        Get-Item $t
    }
Record 'shawl' $f

# -----------------------------------------------------------------------------
# Python -- newest 3.13.x amd64 installer.
# -----------------------------------------------------------------------------
$f = Resolve-OrKeep 'Python 3.13 (newest patch)' `
    -Existing { Get-ChildItem $inst -Filter 'python-3.13.*-amd64.exe' -ErrorAction SilentlyContinue |
                Sort-Object { [version](($_.BaseName -replace 'python-','' -replace '-amd64','')) } -Descending |
                Select-Object -First 1 } `
    -Fetch {
        $html = (Invoke-WebRequest 'https://www.python.org/ftp/python/' -UseBasicParsing -TimeoutSec 30).Content
        $ver = [regex]::Matches($html, '(?<=href=")3\.13\.\d+(?=/")') |
            ForEach-Object { [version]$_.Value } | Sort-Object -Unique | Select-Object -Last 1
        if (-not $ver) { throw 'no 3.13.x dir found in python.org FTP index' }
        $script:resolved['python'] = "$ver"
        $t = Join-Path $inst "python-$ver-amd64.exe"
        if ((Test-Path $t) -and -not $Force) {
            Write-Host "   current: python-$ver-amd64.exe" -f Green
        } else {
            Write-Host "   downloading python-$ver-amd64.exe..." -f Gray
            Save-Url "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe" $t -TimeoutSec 600 -MinBytes 20MB
            Write-Host "   done." -f Green
        }
        Get-ChildItem $inst -Filter 'python-3.13.*-amd64.exe' | Where-Object FullName -ne $t |
            ForEach-Object { Remove-Item $_.FullName -Force; Write-Host "   removed old $($_.Name)" -f Gray }
        Get-Item $t
    }
Record 'python' $f

# -----------------------------------------------------------------------------
# pip wheels -- PINNED to requirements.lock.txt, fetched for win_amd64 / cp313.
# Needs Python on the build host. Re-downloaded only when the lock changes.
# -----------------------------------------------------------------------------
Write-Host "-- Probe pip wheels (pinned to requirements.lock.txt)" -f Cyan
try {
    if (-not (Test-Path $lock)) { throw "requirements.lock.txt not found at $lock" }
    $lockHash   = (Get-FileHash $lock -Algorithm SHA256).Hash
    $haveWheels = Get-ChildItem $wheels -Filter '*.whl' -ErrorAction SilentlyContinue
    if ($haveWheels -and -not $Force -and ($manifestOld.items.wheels.lockHash -eq $lockHash)) {
        Write-Host "   current: $($haveWheels.Count) wheels (lock unchanged)" -f Green
    } elseif ($Offline) {
        if (-not $haveWheels) { throw 'offline and no wheels present' }
        Write-Host "   offline: keeping $($haveWheels.Count) existing wheels" -f Yellow
    } else {
        $py = Get-Command python -ErrorAction SilentlyContinue
        if (-not $py) { throw 'python not on PATH (needed to download the locked wheels)' }
        $wheelsNew = "${wheels}.new"
        if (Test-Path $wheelsNew) { Remove-Item $wheelsNew -Recurse -Force }
        New-Item -ItemType Directory -Force $wheelsNew | Out-Null
        Write-Host "   running pip download against the lock..." -f Gray
        & python -m pip download -r $lock -d $wheelsNew --platform win_amd64 --python-version 313 --only-binary=:all: --implementation cp
        if ($LASTEXITCODE -ne 0) { Remove-Item $wheelsNew -Recurse -Force -ErrorAction SilentlyContinue; throw "pip download exited $LASTEXITCODE" }
        if ((Get-ChildItem $wheelsNew -Filter '*.whl' -ErrorAction SilentlyContinue).Count -lt 1) {
            Remove-Item $wheelsNew -Recurse -Force -ErrorAction SilentlyContinue; throw 'pip download produced no wheels'
        }
        if (Test-Path $wheels) { Remove-Item $wheels -Recurse -Force }
        Move-Item $wheelsNew $wheels
        Write-Host "   done." -f Green
    }
    $cnt = (Get-ChildItem $wheels -Filter '*.whl' -ErrorAction SilentlyContinue).Count
    $manifest.items['wheels'] = [ordered]@{ version = "lock:$($lockHash.Substring(0,12))"; lockHash = $lockHash; count = $cnt }
} catch {
    $script:errors += "wheels: $($_.Exception.Message)"
    Write-Host "   ERROR: $($_.Exception.Message)" -f Red
}
Write-Host ""

# -----------------------------------------------------------------------------
# Mosquitto -- newest 64-bit Windows installer (test broker). Version scraped
# from the download page; no published per-file hash, so size-floor only.
# -----------------------------------------------------------------------------
$f = Resolve-OrKeep 'Mosquitto (test broker, newest)' `
    -Existing { Get-ChildItem $inst -Filter 'mosquitto-*-install-windows-x64.exe' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -First 1 } `
    -Fetch {
        $page = (Invoke-WebRequest 'https://mosquitto.org/download/' -UseBasicParsing -TimeoutSec 30).Content
        $ver = [regex]::Matches($page, 'mosquitto-(\d+\.\d+\.\d+)-install-windows-x64\.exe') |
            ForEach-Object { $_.Groups[1].Value } | Sort-Object { [version]$_ } | Select-Object -Last 1
        if (-not $ver) { throw 'could not scrape a mosquitto win64 version from download page' }
        $script:resolved['mosquitto'] = $ver
        $t = Join-Path $inst "mosquitto-$ver-install-windows-x64.exe"
        if ((Test-Path $t) -and -not $Force) {
            Write-Host "   current: mosquitto-$ver-install-windows-x64.exe" -f Green
        } else {
            Write-Host "   downloading mosquitto-$ver-install-windows-x64.exe..." -f Gray
            Save-Url "https://mosquitto.org/files/binary/win64/mosquitto-$ver-install-windows-x64.exe" $t -TimeoutSec 300 -MinBytes 200KB
            Write-Host "   done." -f Green
        }
        Get-ChildItem $inst -Filter 'mosquitto-*-install-windows-x64.exe' | Where-Object FullName -ne $t |
            ForEach-Object { Remove-Item $_.FullName -Force; Write-Host "   removed old $($_.Name)" -f Gray }
        Get-Item $t
    }
Record 'mosquitto' $f

# -----------------------------------------------------------------------------
# Git for Windows -- latest 64-bit installer (+ SHA256 from the release digest).
# -----------------------------------------------------------------------------
$f = Resolve-OrKeep 'Git for Windows (latest, hash-verified)' `
    -Existing { Get-ChildItem $inst -Filter 'Git-*-64-bit.exe' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -First 1 } `
    -Fetch {
        $rel   = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -TimeoutSec 30
        $asset = $rel.assets | Where-Object { $_.name -match '^Git-.*-64-bit\.exe$' } | Select-Object -First 1
        if (-not $asset) { throw 'no Git-*-64-bit.exe asset in latest release' }
        $script:resolved['git'] = $rel.tag_name
        $t = Join-Path $inst $asset.name
        # The GitHub asset 'digest' field is "sha256:<hex>" when present.
        $wantSha = $null
        if ($asset.digest -and $asset.digest -match '^sha256:([0-9a-fA-F]{64})$') { $wantSha = $Matches[1].ToUpper() }
        if ((Test-Path $t) -and -not $Force) {
            Write-Host "   current: $($asset.name)" -f Green
        } else {
            Write-Host "   downloading $($asset.name)..." -f Gray
            Save-Url $asset.browser_download_url $t -TimeoutSec 600 -MinBytes 20MB
            if ($wantSha) {
                $got = (Get-FileHash $t -Algorithm SHA256).Hash
                if ($got -ne $wantSha) { Remove-Item $t -Force; throw "Git SHA256 mismatch (got $got, expected $wantSha)" }
                Write-Host "   done (SHA256 verified)." -f Green
            } else {
                Write-Host "   done (no digest published; size-checked only)." -f Yellow
            }
        }
        Get-ChildItem $inst -Filter 'Git-*-64-bit.exe' | Where-Object FullName -ne $t |
            ForEach-Object { Remove-Item $_.FullName -Force; Write-Host "   removed old $($_.Name)" -f Gray }
        Get-Item $t
    }
Record 'git' $f

# -----------------------------------------------------------------------------
# Manifest + summary.
# -----------------------------------------------------------------------------
$manifest.errors = $script:errors
$manifest | ConvertTo-Json -Depth 6 | Set-Content $manifestPath -Encoding utf8
Write-Host "Manifest written: $manifestPath" -f Gray
Write-Host ""
Write-Host "Resolved versions:" -f Green
foreach ($k in $manifest.items.Keys) {
    $it = $manifest.items[$k]
    if ($k -eq 'wheels') { Write-Host ("  {0,-10} {1} wheels" -f $k, $it.count) -f Gray }
    else { Write-Host ("  {0,-10} {1}  ({2})" -f $k, $it.version, $it.file) -f Gray }
}
Write-Host ""

if ($script:errors.Count -gt 0) {
    Write-Host "prepare-offline finished with $($script:errors.Count) error(s):" -f Red
    $script:errors | ForEach-Object { Write-Host "  - $_" -f Red }
    exit 1
}
Write-Host "Offline bundle ready. install-windows.ps1 now runs without internet." -f Green
exit 0
