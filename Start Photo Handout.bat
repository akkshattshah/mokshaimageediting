@echo off
title Photo Handout  -  keep this window open
cd /d "%~dp0"
set HANDOUT_OPEN=1
echo ============================================================
echo    PHOTO HANDOUT is starting...
echo.
echo    A browser tab will open automatically in a few seconds.
echo    If it doesn't, open:  http://127.0.0.1:5000
echo.
echo    KEEP THIS WINDOW OPEN while you work.
echo    Close it (or press Ctrl+C) when you're done, to stop.
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py webform.py
) else (
    python webform.py
)
echo.
echo Photo Handout has stopped. You can close this window.
pause
