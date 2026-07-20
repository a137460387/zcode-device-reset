@echo off
chcp 65001 >nul
REM Thin launcher: runs the Python implementation with the same name.
REM This lets users double-click the .bat while all logic lives in the .py file.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM Prefer `python`, fall back to `py` launcher (bundled with Python installer on Windows).
where python >nul 2>&1
if %errorlevel%==0 (
    python "%SCRIPT_DIR%reset-zcode-device.py" %*
    goto :done
)

where py >nul 2>&1
if %errorlevel%==0 (
    py "%SCRIPT_DIR%reset-zcode-device.py" %*
    goto :done
)

echo Python was not found on PATH.
echo Install Python from https://www.python.org/ or add it to PATH.
pause
exit /b 1

:done
endlocal
