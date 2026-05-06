#!/bin/bash
# RUN_SCHEDULER.sh — Start the AI Team agents (scheduler + dashboard)
#
# This script starts both the dashboard and scheduler processes.
# The dashboard serves the web UI on http://localhost:5000
# The scheduler runs the agent cron jobs
# Close all windows or press Ctrl+C to stop.

cd "$(dirname "$0")"

echo "=========================================="
echo "AI Team Agent System"
echo "=========================================="
echo ""

echo "Starting Dashboard on http://localhost:5000..."
echo ""

# Start dashboard in background
nohup python dashboard/run_dashboard.py > /dev/null 2>&1 &
DASHBOARD_PID=$!
echo "Dashboard started with PID: $DASHBOARD_PID"

# Give dashboard time to initialize
sleep 2

echo "Starting Agent Scheduler..."
echo ""
echo "Logs are written to: logs/scheduler/general.log"
echo ""

# Start scheduler in foreground (will run until interrupted)
python scripts/scheduler.py

# Cleanup dashboard on exit
echo ""
echo "Stopping dashboard (PID: $DASHBOARD_PID)..."
kill $DASHBOARD_PID 2>/dev/null
