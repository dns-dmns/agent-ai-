@echo off
echo ======================================================
echo  Antigravity QA Platform Server Launcher
echo ======================================================
echo.

:: Automatically free port 8000 if occupied by any lingering process
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo Clearing existing process on port 8000 (PID: %%a)...
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting Server on http://127.0.0.1:8000 ...
.venv\Scripts\python.exe -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000 --reload
pause
