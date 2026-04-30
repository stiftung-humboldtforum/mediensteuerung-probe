#Requires -Version 5
<#
.SYNOPSIS
    Hardware-Smoke-Test fuer Humboldt-Probe auf einem Windows-Kiosk-PC.

.DESCRIPTION
    Laeuft die Windows-spezifischen Methoden (pycaw, LibreHardwareMonitor,
    Win32 EnumDisplaySettings, psutil) direkt aus — ohne MQTT. Reine
    Hardware-/OS-Verifikation.

    Auszufuehren NACH Deploy auf der Ziel-Hardware. WICHTIG:
    LibreHardwareMonitor braucht **Administrator-Rechte** fuer voll-
    staendige Sensor-Listen — im NSSM-Service-Account entsprechend
    setzen.

.PARAMETER SkipAudio
    Ueberspringt den Audio-Mute-Toggle-Test (fuer Live-Betrieb).

.PARAMETER PythonExe
    Pfad zum Python-Interpreter. Default: C:\Program Files\Python313\python.exe

.EXAMPLE
    .\scripts\hardware-test-windows.ps1

.EXAMPLE
    .\scripts\hardware-test-windows.ps1 -SkipAudio
#>
param(
    [switch]$SkipAudio,
    [string]$PythonExe = "C:\Program Files\Python313\python.exe"
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Resolve-Path "$PSScriptRoot\.."
$Pass = 0
$Fail = 0

# Python wahlen — venv falls vorhanden
if (Test-Path "$RepoRoot\.venv\Scripts\python.exe") {
    $Py = "$RepoRoot\.venv\Scripts\python.exe"
} elseif (Test-Path $PythonExe) {
    $Py = $PythonExe
} else {
    $Py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Py) { Write-Error "No Python found"; exit 2 }
}

$env:PYTHONPATH = "$RepoRoot\src"

function Step {
    param(
        [string]$Name,
        [string]$Code,
        [string]$ExpectRegex = '.'
    )
    try {
        $out = & $Py -c $Code 2>&1 | Out-String
        $out = $out.Trim()
        if ($out -match $ExpectRegex) {
            Write-Host "[OK  ] $Name — $($out.Substring(0, [Math]::Min(100, $out.Length)))"
            $script:Pass++
        } else {
            Write-Host "[FAIL] $Name — output didn't match /$ExpectRegex/: $out"
            $script:Fail++
        }
    } catch {
        Write-Host "[FAIL] $Name — exception: $_"
        $script:Fail++
    }
}

Write-Host "Windows hardware smoke test on $env:COMPUTERNAME — Python: $Py"
Write-Host ""

# --- Admin check (LHM needs it) ------------------------------------------
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $IsAdmin) {
    Write-Host "[WARN] not running as Administrator — LHM tests will likely return empty {}"
}

# --- Display (Win32 EnumDisplaySettings) ---------------------------------
Step 'display: Win32 EnumDisplaySettings returns mode' `
     'from methods._win32 import display; print(display())' `
     '\d+x\d+'

# --- Uptime (psutil) -----------------------------------------------------
Step 'uptime: psutil-based seconds since boot' `
     'from methods._win32 import uptime; print(uptime())' `
     '^\d+\.\d+'

# --- Temperatures (LibreHardwareMonitor) ---------------------------------
# LHM returns {} non-Admin; mit Admin sollten CPU/GPU temps drin sein.
Step 'temperatures: LHM returns dict (Admin needed for non-empty)' `
     'from methods._win32 import temperatures; r = temperatures(); print(type(r).__name__, len(r), r)' `
     '^dict'

Step 'fans: LHM returns dict (Admin needed for non-empty)' `
     'from methods._win32 import fans; r = fans(); print(type(r).__name__, len(r), r)' `
     '^dict'

# --- Audio (pycaw) — invasiv ---------------------------------------------
if ($SkipAudio) {
    Write-Host "[SKIP] audio toggle (SkipAudio set)"
} else {
    Step 'audio: pycaw IAudioEndpointVolume.GetMute reads state' `
         'from methods._win32 import is_muted; print(is_muted())' `
         '^(True|False)$'

    Step 'audio: mute toggle works' `
         @'
from methods._win32 import is_muted, mute, unmute
before = is_muted()
mute(); after_mute = is_muted()
unmute(); after_unmute = is_muted()
assert after_mute is True, f"after mute: {after_mute}"
assert after_unmute is False, f"after unmute: {after_unmute}"
print("toggle ok, restored=", before)
'@ `
         'toggle ok'
}

# --- easire-Detection ----------------------------------------------------
Step 'easire: psutil.process_iter runs' `
     'from methods import easire; r = easire(); print("running" if r else "not running")' `
     '(running|not running)'

# --- shutdown.exe permission ---------------------------------------------
# Nicht aufrufen — nur prüfen ob's existiert (Service-Account hat
# typischerweise Rechte).
$shutdownPath = (Get-Command shutdown.exe -ErrorAction SilentlyContinue).Source
if ($shutdownPath) {
    Write-Host "[OK  ] shutdown.exe available at $shutdownPath"
    $Pass++
} else {
    Write-Host "[FAIL] shutdown.exe not on PATH"
    $Fail++
}

Write-Host ""
Write-Host '──────────────────────────────────────────────'
Write-Host "Result: $Pass passed, $Fail failed"
if ($Fail -eq 0) { exit 0 } else { exit 1 }
