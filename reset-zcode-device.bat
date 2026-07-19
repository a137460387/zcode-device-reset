@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "TELEMETRY_FILE=C:\Users\luoguangyu\.zcode\v2\telemetry-state.json"

echo ========================================
echo ZCode Device ID Reset Tool
echo ========================================
echo.

echo [1/3] Reading current deviceMid...
set "OLD_MID=N/A"
if exist "%TELEMETRY_FILE%" (
    for /f "tokens=2 delims=:, " %%a in ('findstr /i "deviceMid" "%TELEMETRY_FILE%"') do (
        set "OLD_MID=%%~a"
    )
    echo    Current deviceMid: !OLD_MID!
) else (
    echo    telemetry-state.json not found.
)

echo.
echo [2/3] Terminating all zcode processes...
taskkill /F /IM "ZCode.exe" >nul 2>&1
taskkill /F /IM "zcode.exe" >nul 2>&1
taskkill /F /IM "zcode-helper.exe" >nul 2>&1
taskkill /F /IM "zcode-cli.exe" >nul 2>&1
timeout /t 3 /nobreak >nul
echo    Done.

echo.
echo [3/3] Deleting telemetry-state.json...
if exist "%TELEMETRY_FILE%" (
    del /f "%TELEMETRY_FILE%"
    echo    Deleted.
) else (
    echo    File not found, skipping.
)

echo.
echo ========================================
echo DONE - Cleanup complete.
echo Old deviceMid: !OLD_MID!
echo Please start ZCode to generate new device ID.
echo ========================================

pause
