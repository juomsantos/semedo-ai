"""
scheduler.py — Background process that invokes all agent scripts on their intervals.

Usage:
    python scripts/scheduler.py

This process runs indefinitely, invoking each agent script on its cron schedule.
Log output goes to logs/scheduler/scheduler.log

Press Ctrl+C to gracefully shut down.
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.logger import AgentLogger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# Agent definitions: (script_name, interval_minutes)
AGENTS = [
    ("agent_orchestrator.py", 1),
    ("agent_coder.py", 2),
    ("agent_research.py", 2),
    ("agent_claude_code.py", 3),
    ("agent_qa.py", 2),
]


class AgentScheduler:
    def __init__(self):
        self.log = AgentLogger("scheduler")
        self.stop_event = Event()
        self.next_run_times = {}
        self._init_schedules()

    def _init_schedules(self):
        """Initialize next run times for all agents."""
        now = datetime.now()
        for script, interval in AGENTS:
            # First run after a small delay to avoid thundering herd
            self.next_run_times[script] = now + timedelta(seconds=5)
            self.log.info(f"Scheduled {script} to run every {interval} minute(s)")

    def _run_agent(self, script: str):
        """Run an agent script as a subprocess."""
        script_path = SCRIPTS_DIR / script
        if not script_path.exists():
            self.log.error(f"Script not found: {script_path}")
            return

        try:
            self.log.info(f"Starting {script}")
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if result.returncode == 0:
                self.log.info(f"Completed {script} (exit code 0)")
            else:
                self.log.error(f"Failed {script} (exit code {result.returncode})")
                if result.stderr:
                    self.log.error(f"  stderr: {result.stderr[:200]}")

        except subprocess.TimeoutExpired:
            self.log.error(f"Timeout {script} (exceeded 300s)")
        except Exception as e:
            self.log.error(f"Exception running {script}: {e}")

    def _schedule_agents(self):
        """Thread that manages scheduling and invocation."""
        self.log.info("Scheduler started")

        while not self.stop_event.wait(1):  # Check every second
            now = datetime.now()

            for script, interval in AGENTS:
                if now >= self.next_run_times[script]:
                    # Schedule next run
                    self.next_run_times[script] = now + timedelta(minutes=interval)

                    # Run agent in a separate thread to avoid blocking
                    Thread(target=self._run_agent, args=(script,), daemon=True).start()

        self.log.info("Scheduler stopped")

    def run(self):
        """Start the scheduler and block until interrupted."""
        try:
            self._schedule_agents()
        except KeyboardInterrupt:
            self.log.info("Shutdown requested (Ctrl+C)")
            self.stop_event.set()
            time.sleep(1)  # Give threads time to clean up
            self.log.info("Scheduler exited cleanly")


def main():
    scheduler = AgentScheduler()
    scheduler.run()


if __name__ == "__main__":
    main()
