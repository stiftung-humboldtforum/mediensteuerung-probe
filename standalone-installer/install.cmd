@echo off
REM =====================================================================
REM Humboldt-Probe -- offline installer launcher.
REM Double-click this file. It self-elevates (UAC), then runs install.ps1
REM from this same folder. No internet required.
REM =====================================================================
setlocal

REM Already elevated? `net session` only succeeds as admin.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights ^(confirm the UAC prompt^)...
    REM Pass the path via an env var so spaces AND apostrophes in the folder
    REM name survive the cmd/PowerShell quote handling. Catch a declined/failed
    REM UAC so the window does not just vanish silently.
    set "SELF=%~f0"
    powershell -NoProfile -Command "try { Start-Process -FilePath $env:SELF -Verb RunAs } catch { exit 1 }"
    if errorlevel 1 (
        echo.
        echo Elevation was declined or failed.
        echo Right-click install.cmd and choose "Run as administrator".
        echo.
        pause
    )
    exit /b
)

echo Installing Humboldt-Probe ^(offline^)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set RC=%errorlevel%
echo.
if "%RC%"=="0" (
    echo === Installation finished OK. ===
) else (
    echo === Installation FAILED ^(exit %RC%^). See the messages above. ===
)
echo.
pause
endlocal
