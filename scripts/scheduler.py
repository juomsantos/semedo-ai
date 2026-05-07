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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.logger import AgentLogger
from shared.ollama_client import OllamaClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# Agent definitions: (script_name, interval_minutes)
AGENTS = [
    ("agent_orchestrator.py", 3),
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

    def _check_ollama_availability(self) -> bool:
        """Check if Ollama server is reachable. Log and print results."""
        client = OllamaClient()
        try:
            is_available = client.is_available()
            if is_available:
                msg = f"✓ Ollama server is available at {client.base_url}"
                self.log.info(msg)
                print(msg)
                return True
            else:
                msg = f"✗ Ollama server is not responding at {client.base_url}"
                self.log.error(msg)
                print(msg)
                return False
        except Exception as e:
            msg = f"✗ Failed to reach Ollama server at {client.base_url}: {e}"
            self.log.error(msg)
            print(msg)
            return False

    def _init_schedules(self):
        """Initialize next run times for all agents."""
        now = datetime.fromtimestamp(time.time(), tz=timezone.utc)
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
            now = datetime.fromtimestamp(time.time(), tz=timezone.utc)

            for script, interval in AGENTS:
                if now >= self.next_run_times[script]:
                    # Schedule next run
                    self.next_run_times[script] = now + timedelta(minutes=interval)

                    # Run agent in a separate thread to avoid blocking
                    Thread(target=self._run_agent, args=(script,), daemon=True).start()

        self.log.info("Scheduler stopped")

    def run(self):
        """Start the scheduler and block until interrupted."""
        # Check Ollama availability before starting scheduler
        print("\n" + "="*60)
        print("AI Team Scheduler — Initializing")
        print("="*60)
        
        if not self._check_ollama_availability():
            msg = "\n⚠ WARNING: Scheduler starting without Ollama connection.\n  Agents will fail until Ollama is available.\n"
            self.log.warning(msg)
            print(msg)
        else:
            print()  # Newline after success message
        
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
