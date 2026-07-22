@echo off
chcp 65001 >nul
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

rem Pick a random available account UID.
rem   - On success random-pick.py prints the UID to stdout (captured below).
rem   - On failure it prints a one-line reason to stderr (shown on the
rem     console, not captured) and produces no stdout, so TARGET_UID stays
rem     empty and we fall through to the failure path.
rem "delims=" captures the whole line in case the UID format ever changes.
set "TARGET_UID="
for /f "delims=" %%i in ('python random-pick.py') do set "TARGET_UID=%%i"
if not defined TARGET_UID goto :failed

python "%SCRIPT_DIR%reset-zcode-device.py" --switch %TARGET_UID%
if %errorlevel%==0 goto :ok

:failed
echo.
echo Account switch failed. See messages above.
endlocal
pause
exit /b 1

:ok
endlocal
exit /b 0
