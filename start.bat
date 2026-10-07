@echo off
setlocal
cd /d "%~dp0"
title AutoColab - Server Logs
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
echo ============================================================
echo AutoColab - server logs
echo Close this window to stop the server and its child processes.
echo Unexpected crashes are retried automatically while this window is open.
echo ============================================================
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_host.ps1"
set "AUTOCOLAB_EXIT_CODE=%ERRORLEVEL%"
if not "%AUTOCOLAB_EXIT_CODE%"=="0" (
    echo.
    if "%AUTOCOLAB_EXIT_CODE%"=="2" (
        echo AutoColab is already running. A second server was not started.
    ) else if "%AUTOCOLAB_EXIT_CODE%"=="3" (
        echo AutoColab configuration needs correction. See the messages above.
    ) else (
        echo AutoColab stopped with an error. See the messages above.
    )
    pause
)
exit /b %AUTOCOLAB_EXIT_CODE%
