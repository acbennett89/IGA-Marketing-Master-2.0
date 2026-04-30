@echo off
REM ============================================================
REM Diagnose.bat -- IGA Marketing Master 2.0 diagnostic launcher
REM ------------------------------------------------------------
REM Runs scripts\diagnose.py against the venv Python with a
REM visible console window that stays open until you press a key.
REM
REM Default: real extraction against all PDFs in
REM   Testing and Example Library\diagnostic_inputs\
REM
REM To pass flags (--check, --single, --force-opus), edit the
REM line below or run scripts\diagnose.py directly from a
REM PowerShell prompt with the venv active.
REM ============================================================

setlocal

set "PROJ_ROOT=%~dp0"
set "VENV_PY=%PROJ_ROOT%.venv\Scripts\python.exe"
set "SCRIPT=%PROJ_ROOT%scripts\diagnose.py"

if not exist "%VENV_PY%" (
    echo.
    echo The Python virtual environment was not found at:
    echo   %VENV_PY%
    echo.
    echo Run scripts\bootstrap.ps1 first or double-click Launcher.vbs once
    echo to set up the environment.
    echo.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo.
    echo Diagnostic script not found at:
    echo   %SCRIPT%
    echo.
    pause
    exit /b 1
)

REM Pass through any extra arguments (so you can do: Diagnose.bat --check).
"%VENV_PY%" "%SCRIPT%" %*

set "RC=%ERRORLEVEL%"
echo.
echo Diagnostic exited with code %RC%.
pause
exit /b %RC%
