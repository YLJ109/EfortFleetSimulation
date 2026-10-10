@echo off
chcp 65001 >nul
REM EFORT Web Monitoring - backend (FastAPI + WebSocket + SQLite)
cd /d "%~dp0.."
cd backend
call .venv\Scripts\activate.bat
REM ★★ 2026-10-10 安全加固（静态评审 A3）：默认**只绑本机**。
REM   原来硬写 --host 0.0.0.0：登录口与全部 API 都摊在整个局域网（虽有令牌+限流，
REM   但没必要把攻击面放到网络上）。现场确实需要从别的电脑访问时，先设环境变量
REM   EFORT_HOST=0.0.0.0 再跑本脚本（或直接改这一行）。
if "%EFORT_HOST%"=="" set "EFORT_HOST=127.0.0.1"
echo   bind: %EFORT_HOST%:8000
python -m uvicorn app.main:app --host %EFORT_HOST% --port 8000
pause
