@echo off
title First-time setup
cd /d "%~dp0"
echo ============================================================
echo    ONE-TIME SETUP - installs the components this tool needs.
echo    You only need to run this once on a new computer.
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install -r requirements.txt
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python -m pip install -r requirements.txt
    ) else (
        echo Python was not found on this computer.
        echo Please install Python from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during install, then run this again.
        pause
        exit /b 1
    )
)
echo.
echo Setup complete. You can now use "Start Photo Handout".
pause
