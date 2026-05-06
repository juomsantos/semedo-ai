@echo off
REM RUN_SCHEDULER.bat — Start the AI Team agents (scheduler + dashboard)
REM
REM This batch file starts both the dashboard and scheduler processes.
REM The dashboard serves the web UI on http://localhost:5000
REM The scheduler runs the agent cron jobs
REM Close all windows or press Ctrl+C to stop.

cd /d "%~dp0"

echo ========================================
echo AI Team Agent System
echo ========================================
echo.

echo Starting Dashboard on http://localhost:5000...
echo.

start cmd /k "python dashboard\run_dashboard.py"

timeout /t 2 /nobreak >nul

echo Starting Agent Scheduler...
echo.
echo Logs are written to: logs\scheduler\general.log
echo.

python scripts\scheduler.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Scheduler failed to start.
    echo Make sure Python is installed and in your PATH.
    pause
)
