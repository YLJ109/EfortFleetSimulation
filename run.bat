@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title EFORT Web Monitoring
cd /d "%~dp0"

REM ====================================================================
REM  EFORT Web Monitoring - one-click launcher  (ASCII only)
REM
REM  Double-click (no args) = clean old procs, start vision + backend,
REM  then open the browser at http://localhost:8000
REM
REM  Usage: run.bat [all|backend|camera|stop|build|help]
REM ====================================================================

if "%~1"==""        goto START_ALL
if /i "%~1"=="all"     goto START_ALL
if /i "%~1"=="backend" goto START_BACKEND
if /i "%~1"=="camera"  goto START_VISION
if /i "%~1"=="vision"  goto START_VISION
if /i "%~1"=="stop"    goto STOP_ALL
if /i "%~1"=="build"   goto REBUILD
if /i "%~1"=="help"    goto USAGE
if /i "%~1"=="/?"      goto USAGE
goto USAGE

REM ============================ ACTIONS ============================

:START_ALL
call :ENSURE_FRONTEND
echo   Cleaning old services (if any) ...
call :KILL_PORT 8000
call :KILL_PORT 8100
REM 2026-10-10: the two KILL_PORT calls above only kill the process that OWNS the port.
REM On Windows SO_REUSEADDR allows two processes to bind the same port, so the one
REM that never gets a connection becomes an IDLE ORPHAN (CPU 0s) and piles up
REM (seen on site: 2x vision + 2x backend, one of each idle). Kill by command line too.
call :KILL_ORPHANS camera_service.py
call :KILL_ORPHANS uvicorn
call :START_VISION_ONLY
call :START_BACKEND_ONLY
timeout /t 3 >nul
echo   Opening browser: http://localhost:8000
start "" http://localhost:8000
exit /b 0

:START_BACKEND
call :ENSURE_FRONTEND
call :START_BACKEND_ONLY
exit /b 0

:START_VISION
call :START_VISION_ONLY
exit /b 0

:START_BACKEND_ONLY
call :KILL_PORT 8000
echo   Starting backend :8000 ...
start "EFORT Backend :8000" cmd /k "%~dp0scripts\start_backend.bat"
timeout /t 2 >nul
exit /b 0

:START_VISION_ONLY
call :KILL_PORT 8100
echo   Starting vision :8100 ...
start "EFORT Vision :8100" cmd /k "%~dp0scripts\start_camera.bat"
timeout /t 2 >nul
exit /b 0

:STOP_ALL
echo   Stopping services ...
call :KILL_PORT 8000
call :KILL_PORT 8100
call :KILL_ORPHANS camera_service.py
call :KILL_ORPHANS uvicorn
echo   Done.
exit /b 0

:REBUILD
echo   Rebuilding frontend ...
pushd frontend
call npm run build
popd
echo   Build finished.
exit /b 0

:USAGE
echo EFORT Web Monitoring launcher
echo.
echo    run.bat             start all + open browser (recommended)
echo    run.bat backend     start backend only :8000
echo    run.bat camera      start vision service only :8100
echo    run.bat stop        stop all services
echo    run.bat build       rebuild frontend
echo.
exit /b 0

REM ============================ HELPERS ============================

:ENSURE_FRONTEND
if not exist "frontend\dist\index.html" (
    echo   Frontend not built - building now ...
    pushd frontend
    call npm run build
    popd
)
exit /b 0

:KILL_PORT
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%~1" ^| findstr LISTENING') do (
    echo     stopping pid %%p on port %~1
    taskkill /PID %%p /F >nul 2>&1
)
exit /b 0

REM Kill our own service processes by COMMAND LINE (covers KILL_PORT's blind spot:
REM the idle orphan that does not own the port). Only matches python.exe + keyword.
REM Call: call :KILL_ORPHANS camera_service.py
REM WARNING about quoting: cmd does NOT understand single quotes, so
REM   -Command '... | ...'  makes cmd split on the pipe
REM   (seen: "'Where-Object' is not recognized as an internal or external command").
REM So: outer double quotes protect the pipe; all PowerShell strings use single
REM quotes ('' escapes one single quote).
:KILL_ORPHANS
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter ('Name=''python.exe''') | Where-Object { $_.CommandLine -like ('*' + '%~1' + '*') } | ForEach-Object { Write-Host ('    stopping orphan pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }" 2>nul
exit /b 0
