@echo off
setlocal
chcp 65001 >nul
REM =====================================================================
REM  EFORT Vision Service  (color sorting: OpenCV only)
REM  Runs camera/camera_service.py on the camera venv.
REM
REM  Camera: ordinary USB webcam by default (OpenCV/DirectShow).
REM          Hikrobot GigE via the MVS SDK wrapper is OPTIONAL / legacy —
REM          you do NOT need it (nor any MVS client program) for USB cameras.
REM          When no GigE camera is connected you WILL see
REM          "  [海康工业相机(MVS)] 发现 0 个设备"  -> that is normal.
REM
REM  Overridable via .env (project root) or real environment variables:
REM    EFORT_CAMERA_PYTHON      interpreter path
REM    EFORT_MVS_SDK_DIR        dir containing MvCameraControl_class.py (optional)
REM    EFORT_MVS_RUNTIME_DIR    MVS runtime DLL dir (optional)
REM    CAMERA_PORT              HTTP port (default 8100)
REM    EFORT_CAMERA_HOST        bind address (default 127.0.0.1; 0.0.0.0 = LAN)
REM =====================================================================
cd /d "%~dp0.."

REM ---- load .env (KEY=VALUE, '#' comments); real env vars take precedence ----
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if not "%%~a"=="" if not defined %%~a set "%%~a=%%~b"
    )
)

REM ★★ 2026-10-10：默认 EFORT_CAMERA_KIND=usb —— **不启用海康工业相机**，
REM   连 MVS 运行时都不塞进 PATH（USB 摄像头用不到它，还会让启动输出出现
REM   "海康工业相机(MVS) 发现 0 个设备"那种看着像报错的噪音）。
if "%EFORT_CAMERA_KIND%"=="" set "EFORT_CAMERA_KIND=usb"
if /i not "%EFORT_CAMERA_KIND%"=="usb" (
    set "MVCAM_RUNTIME=%EFORT_MVS_RUNTIME_DIR%"
    if "%MVCAM_RUNTIME%"=="" set "MVCAM_RUNTIME=C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
    if exist "%MVCAM_RUNTIME%" set "PATH=%MVCAM_RUNTIME%;%PATH%"
)

if "%CAMERA_PORT%"=="" set "CAMERA_PORT=8100"

set "CAM_PY=%EFORT_CAMERA_PYTHON%"
if "%CAM_PY%"=="" set "CAM_PY=D:\EFORT_Projects\EFORT_Camera_Python_OpenCv\venv\Scripts\python.exe"

if not exist "%CAM_PY%" (
    echo.
    echo [ERROR] vision interpreter not found:
    echo         %CAM_PY%
    echo.
    echo   Create an environment with camera\requirements.txt, install the MVS
    echo   python wrapper, then set EFORT_CAMERA_PYTHON in .env
    echo   ^(copy .env.example to .env and edit^).
    echo.
    pause
    exit /b 1
)

echo ============================================
echo  EFORT Vision Service
echo  Camera : USB webcam (OpenCV). Hikrobot GigE = optional.
echo  kind   : %EFORT_CAMERA_KIND%   (usb = 不启用工业相机; mvs/auto = 才用海康)
echo  python : %CAM_PY%
echo  url    : http://%EFORT_CAMERA_HOST%:%CAMERA_PORT%   (default host 127.0.0.1)
echo  NOTE   : if port %CAMERA_PORT% is already in use, the service is ALREADY running -
echo           do not start a second instance (it cannot open a held camera).
echo ============================================
echo.

"%CAM_PY%" camera\camera_service.py

if errorlevel 1 (
    echo.
    echo [ERROR] service exited with code %errorlevel%
    echo Check: camera powered on, cable on the 192.168.1.x NIC,
    echo        and camera not busy inside the MVS client.
    pause
)
