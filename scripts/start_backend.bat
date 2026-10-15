@echo off
chcp 65001 >nul
REM EFORT Web Monitoring - backend (FastAPI + WebSocket + SQLite)
cd /d "%~dp0.."
cd backend
call .venv\Scripts\activate.bat

REM ---- fleet big-screen simulation ----
REM This PC has no robot cabling attached, so the fleet page would show 12 offline
REM cards. These two switches start the machine-room collector on SYNTHETIC frames
REM only: no TCP connection is opened and no write channel is enabled.
REM >>> Delete both lines (or set them to 0) BEFORE connecting real robots. <<<
set EFORT_FLEET=1
set EFORT_FLEET_SIM=1
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
