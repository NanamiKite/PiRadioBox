@echo off
setlocal
cd /d "%~dp0"
title PiRadioBox

set "PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
)

if not defined PYTHON (
    echo Could not find Python 3.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
    echo During setup, enable "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Setting up the app's private Python environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :setup_failed
)

echo Checking Python packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :setup_failed

echo Starting PiRadioBox...
".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo PiRadioBox reported a startup error. Check piradiobox.log for details.
    pause
)
exit /b

:setup_failed
echo.
echo Could not prepare Python packages. Check your internet connection and Python installation.
pause
exit /b 1
