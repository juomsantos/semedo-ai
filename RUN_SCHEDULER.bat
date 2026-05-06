@echo off
REM RUN_SCHEDULER.bat — Start the agent scheduler process
REM
REM This batch file starts the Python scheduler in a new window.
REM The scheduler will run indefinitely, invoking agents on their intervals.
REM Close the window or press Ctrl+C to stop.

cd /d "%~dp0"

echo Starting AI Team Agent Scheduler...
echo Agents will poll at intervals: Orchestrator (1min), Coder (2min), Research (2min), ClaudeCode (3min)
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
