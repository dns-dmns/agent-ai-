@echo off
:: Always run from the folder this script lives in, so the launcher works no
:: matter which drive or directory the project was copied to.
cd /d "%~dp0"

echo ======================================================
echo  AI QA Automation Platform - Server Launcher
echo ======================================================
echo  Workspace: %~dp0
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No virtual environment found at .venv
    echo         Run:  python -m venv .venv
    echo               .venv\Scripts\activate
    echo               pip install -r requirements.txt
    echo               playwright install chromium
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARN] No .env file found - AI generation will be disabled.
    echo        Copy .env.example to .env and add your GEMINI_API_KEY.
    echo.
)

:: Automatically free port 8000 if occupied by any lingering process
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo Clearing existing process on port 8000 (PID: %%a)...
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting Server on http://127.0.0.1:8000 ...
:: qa_agent.py is a thin shim over app\main.py, so this command is stable.
.venv\Scripts\python.exe -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000 --reload
pause
