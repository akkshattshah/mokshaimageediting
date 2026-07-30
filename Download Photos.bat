@echo off
title Download Photos
cd /d "%~dp0"
echo ============================================================
echo    DOWNLOAD PHOTOS from the client's storage into the pool.
echo ============================================================
echo.
set /p brand="Brand name (e.g. catchall_ireland): "
set /p count="How many photos to download: "
echo.
echo Downloading %count% photo(s) for "%brand%"...
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py download_s3.py %brand% --photos %count%
) else (
    python download_s3.py %brand% --photos %count%
)
echo.
echo Done. These photos are now available in the Photo Handout form.
pause
