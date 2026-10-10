@echo off
setlocal
chcp 65001 >nul
title EFORT Web Monitoring - environment check
cd /d "%~dp0.."

REM =====================================================================
REM  EFORT Web Monitoring - environment doctor
REM
REM  Read-only self check. Tells you exactly what is missing before you
REM  try to launch. PASS = ready, WARN = optional, FAIL = must fix.
REM  Exit code 0 = no failures, 1 = at least one failure.
REM
REM  ASCII only on purpose (cmd parses .bat as GBK on Chinese Windows).
REM =====================================================================

set /a OKN=0
set /a WARN=0
set /a FAILN=0

REM ---- pick up .env overrides (real env vars win) ----
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if not "%%~a"=="" if not defined %%~a set "%%~a=%%~b"
    )
)

echo ============================================================
echo   EFORT Web Monitoring - environment check
echo   root: %CD%
echo ============================================================
echo.

echo --- 1. toolchain -------------------------------------------------
call :need_py
call :need_node
echo.

echo --- 2. backend ---------------------------------------------------
call :need_file "backend\requirements.txt"             "backend requirements"
call :need_file "backend\app\main.py"                  "backend entrypoint"
call :need_exe  "backend\.venv\Scripts\python.exe"      "backend venv interpreter"
if exist "backend\.venv\Scripts\python.exe" (
    "backend\.venv\Scripts\python.exe" -c "import fastapi,uvicorn,sqlalchemy,yaml,numpy" >nul 2>&1
    if errorlevel 1 (
        call :warn "backend deps incomplete in venv - run setup.bat"
    ) else (
        call :ok "backend deps importable (fastapi/uvicorn/sqlalchemy/yaml/numpy)"
    )
)
echo.

echo --- 3. frontend --------------------------------------------------
call :need_dir  "frontend\node_modules"                "frontend node_modules"
call :need_file "frontend\node_modules\three\package.json" "three.js"
call :need_file "frontend\node_modules\vite\package.json"  "vite"
call :need_file "frontend\dist\index.html"             "frontend build (dist)"
call :need_file "frontend\tools\check_imports.mjs"     "import guard tool"
echo.

echo --- 4. configuration ---------------------------------------------
call :need_file "config\robot.yaml"                    "robot config"
call :need_file "config\safety.json"                   "safety fence config"
if exist ".env" (
    call :ok ".env present (overrides applied)"
) else (
    call :warn ".env missing - built-in defaults will be used (copy .env.example)"
)
set "EFFSIM=%EFORT_SIMULATE%"
if not defined EFFSIM set "EFFSIM=auto"
echo          EFORT_SIMULATE = %EFFSIM%
set "EFFDB=%EFORT_DB_URL%"
if not defined EFFDB set "EFFDB=(default) data\robot.db"
echo          EFORT_DB_URL   = %EFFDB%
set "EFFPORT=%CAMERA_PORT%"
if not defined EFFPORT set "EFFPORT=8100"
echo          CAMERA_PORT    = %EFFPORT%
echo.

echo --- 5. assets ----------------------------------------------------
call :need_file "frontend\public\models\robot_full.glb" "official GLB model"
if exist "frontend\public\models\robot_full.glb" (
    for %%f in ("frontend\public\models\robot_full.glb") do set "GLBSZ=%%~zf"
) else (
    set "GLBSZ=0"
)
if "%GLBSZ%"=="0" (
    call :warn "GLB empty/missing - the 3D view falls back to the procedural model"
) else (
    call :ok "GLB size ok"
)
call :need_file "camera\camera_service.py"             "vision service"
call :need_file "camera\vision_color.py"               "color recognition (OpenCV)"
REM YOLO is DISABLED by default (color sorting uses OpenCV only), so the model file is
REM OPTIONAL - keep it a WARN, do not report FAIL. (Old text implied it was required.)
call :need_chk  "camera\models\yolo26n.pt"             "YOLO model (OPTIONAL - disabled by default)"
call :need_chk  "docs"                                 "docs folder"
echo.

echo --- 6. vision interpreter ----------------------------------------
set "CPY=%EFORT_CAMERA_PYTHON%"
if not defined CPY set "CPY=D:\EFORT_Projects\EFORT_Camera_Python_OpenCv\venv\Scripts\python.exe"
if exist "%CPY%" (
    call :ok "vision interpreter found"
    echo          %CPY%
) else (
    call :warn "vision interpreter missing - vision tab will not work"
    echo          expected: %CPY%
    echo          fix: set EFORT_CAMERA_PYTHON in .env
)
set "MVSR=%EFORT_MVS_RUNTIME_DIR%"
if not defined MVSR set "MVSR=C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
if exist "%MVSR%" (
    call :ok "MVS runtime DLLs found"
) else (
    call :warn "MVS runtime DLLs missing - vision service cannot open the camera"
)
set "SDKD=%EFORT_MVS_SDK_DIR%"
if not defined SDKD set "SDKD=D:\EFORT_Projects\EFORT_Camera_Python_OpenCv\src\sdk\MvImport"
if exist "%SDKD%\MvCameraControl_class.py" (
    call :ok "MVS python wrapper found"
) else (
    call :warn "MVS python wrapper missing - set EFORT_MVS_SDK_DIR in .env"
)
echo.

echo --- 7. runtime ---------------------------------------------------
call :need_dir "data"                                  "data dir"
call :need_dir "logs"                                  "logs dir"
call :port_state 8000 "backend"
call :port_state 8100 "vision"
REM ★ 2026-10-10: also count "how many service instances" - Windows allows two processes to
REM bind the SAME port (SO_REUSEADDR); the one that never gets a connection becomes an idle
REM orphan and piles up. Quoting note: cmd does NOT understand single quotes, so the outer
REM quotes MUST be double (otherwise the | is treated as a cmd pipe). See run.bat KILL_ORPHANS.
powershell -NoProfile -Command "$p=@(Get-CimInstance Win32_Process -Filter ('Name=''python.exe''') | Where-Object { $_.CommandLine -match 'camera_service\.py|uvicorn' }); Write-Host ('  INFO  service python processes: ' + $p.Count); if ($p.Count -gt 2) { Write-Host '  WARN  more than 2 instances (stale orphan?) - run: run.bat stop' }" 2>nul
echo.

echo ============================================================
echo   RESULT:  %OKN% PASS / %WARN% WARN / %FAILN% FAIL
echo ============================================================
if %FAILN% GTR 0 (
    echo.
    echo   Some checks FAILED. Run setup.bat, then re-run doctor.bat.
    echo.
    pause
    exit /b 1
)
echo.
echo   Ready. Launch with:  run.bat
echo.
pause
exit /b 0


REM ======================= subroutines =======================

:ok
echo   PASS  %~1
set /a OKN+=1
exit /b 0

:warn
echo   WARN  %~1
set /a WARN+=1
exit /b 0

:fail
echo   FAIL  %~1
set /a FAILN+=1
exit /b 0

:need_py
set "PYV="
for /f "delims=" %%v in ('python --version 2^>nul') do set "PYV=%%v"
if defined PYV (
    call :ok "python: %PYV%"
) else (
    call :warn "python not on PATH (only needed by setup.bat)"
)
exit /b 0

:need_node
set "NODEV="
for /f "delims=" %%v in ('node -v 2^>nul') do set "NODEV=%%v"
if defined NODEV (
    call :ok "node: %NODEV%"
) else (
    call :fail "node not on PATH - frontend cannot be built"
)
exit /b 0

:need_file
if exist "%~1" (
    call :ok "%~2"
) else (
    call :fail "%~2 missing: %~1"
)
exit /b 0

:need_exe
if exist "%~1" (
    call :ok "%~2"
) else (
    call :fail "%~2 missing: %~1  (run setup.bat)"
)
exit /b 0

:need_dir
if exist "%~1\" (
    call :ok "%~2"
) else (
    call :fail "%~2 missing: %~1"
)
exit /b 0

:need_chk
REM optional asset: present -> PASS note, absent -> WARN.
REM Message goes through the quoted call form because a raw "echo %~2" would
REM break the enclosing if-block whenever the text itself contains ")".
if exist "%~1" (
    call :ok "%~2 present"
) else (
    call :warn "%~2 missing: %~1"
)
exit /b 0

:port_state
netstat -ano | findstr ":%~1" | findstr LISTENING >nul 2>&1
if errorlevel 1 (
    echo   ----  port %~1 free    ^(%~2 not running^)
) else (
    echo   PASS  port %~1 in use  ^(%~2 running^)
    set /a OKN+=1
)
exit /b 0
