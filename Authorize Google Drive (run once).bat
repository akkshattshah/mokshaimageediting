@echo off
title Authorize Google Drive - run once
cd /d "%~dp0"
echo ============================================================
echo    GOOGLE DRIVE SIGN-IN  (you only do this once)
echo.
echo    Your browser will open. Pick the Google account that the
echo    photo zips should be uploaded to, then click Allow.
echo.
echo    If Google warns "app is not verified", click
echo    Advanced  ^>  Go to ... (unsafe)  ^>  Allow.
echo    That is normal for an in-house tool.
echo ============================================================
echo.
pause
where py >nul 2>nul
if %errorlevel%==0 (
    py drive_auth.py
) else (
    python drive_auth.py
)
echo.
pause
