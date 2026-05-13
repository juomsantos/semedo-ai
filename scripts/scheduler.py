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
import platform
import shutil
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.logger import AgentLogger
from shared.ollama_client import OllamaClient
from shared.file_watcher import TaskWatcher
from shared.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# Agent definitions: (script_name, interval_minutes)
AGENTS = [
    ("agent_orchestrator.py", 0.5),
    ("agent_coder.py", 1.5),
    ("agent_research.py", 1),
    ("agent_claude_code.py", 2.5),
    ("agent_qa.py", 2),
]


class AgentScheduler:
    def __init__(self):
        self.log = AgentLogger("scheduler")
        self.stop_event = Event()
        self.next_run_times = {}
        self.watcher = None
        self._watcher_enabled = False

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

    def _init_watchers(self):
        """Initialize file system watchers for immediate task detection."""
        try:
            self.watcher = TaskWatcher(coalescence_window=0.5)

            # Watch inbox/ for new tasks submitted by users/dashboard
            inbox = PROJECT_ROOT / "inbox"
            if inbox.exists():
                self.watcher.watch_folder(
                    folder_path=inbox,
                    callback=lambda: self.trigger_agent("agent_orchestrator.py", "file-watcher"),
                    agent_name="orchestrator",
                )
                self.log.info(f"Watching {inbox} for new submitted tasks")

            # Watch validation/ for completed subtasks that need orchestrator review
            validation = PROJECT_ROOT / "validation"
            if validation.exists():
                self.watcher.watch_folder(
                    folder_path=validation,
                    callback=lambda: self.trigger_agent("agent_orchestrator.py", "file-watcher"),
                    agent_name="orchestrator",
                )
                self.log.info(f"Watching {validation} for completed subtasks")

            # Watch worker inboxes
            worker_folders = {
                "agent_coder.py": PROJECT_ROOT / "agents" / "coder" / "inbox",
                "agent_research.py": PROJECT_ROOT / "agents" / "research" / "inbox",
                "agent_qa.py": PROJECT_ROOT / "agents" / "qa" / "inbox",
                "agent_claude_code.py": PROJECT_ROOT / "agents" / "claude-code" / "inbox",
            }

            for script, folder in worker_folders.items():
                if folder.exists():
                    self.watcher.watch_folder(
                        folder_path=folder,
                        callback=lambda s=script: self.trigger_agent(s, "file-watcher"),
                        agent_name=script.replace("agent_", "").replace(".py", ""),
                    )
                    self.log.info(f"Watching {folder} for tasks")

            self.watcher.start()
            self._watcher_enabled = True
            self.log.info("File watcher initialized and started")

        except ImportError:
            self.log.warning(
                "watchdog not installed — file watching disabled. "
                "Run: pip install watchdog>=3.0.0"
            )
        except Exception as e:
            self.log.warning(f"Failed to initialize file watcher: {e} — falling back to timer-only mode")

    def trigger_agent(self, script: str, reason: str = "timer"):
        """
        Trigger an agent to run immediately.

        Args:
            script: Agent script name (e.g., "agent_orchestrator.py")
            reason: Trigger reason for logging (e.g., "timer" or "file-watcher")
        """
        self.log.info(f"Triggering {script} ({reason})")
        now = datetime.fromtimestamp(time.time(), tz=timezone.utc)

        # Reset timer to prevent immediate re-run
        for s, interval in AGENTS:
            if s == script:
                self.next_run_times[script] = now + timedelta(minutes=interval)
                break

        Thread(target=self._run_agent, args=(script,), daemon=True).start()

    def _run_agent(self, script: str):
        """Run an agent script as a subprocess."""
        script_path = SCRIPTS_DIR / script
        if not script_path.exists():
            self.log.error(f"Script not found: {script_path}")
            return

        # Derive agent name from script filename (e.g. agent_research.py → research)
        agent_name = script.replace("agent_", "").replace(".py", "")
        try:
            process_timeout = load_config().agent_process_timeout(agent_name)
        except Exception:
            process_timeout = 300

        try:
            self.log.info(f"Starting {script} (process_timeout={process_timeout}s)")

            # Isolate subprocess from the scheduler's console signal group so that
            # a Ctrl+C or SIGINT reaching the scheduler does not propagate to agents
            # mid-LLM-call (which would orphan tasks in processing/).
            kwargs = {}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=process_timeout,
                **kwargs,
            )

            if result.returncode == 0:
                self.log.info(f"Completed {script} (exit code 0)")
            else:
                self.log.error(f"Failed {script} (exit code {result.returncode})")
                if result.stderr:
                    self.log.error(f"  stderr: {result.stderr[:200]}")

        except subprocess.TimeoutExpired:
            self.log.error(f"Timeout {script} (exceeded {process_timeout}s)")
        except Exception as e:
            self.log.error(f"Exception running {script}: {e}")

    def _schedule_agents(self):
        """Thread that manages scheduling and invocation."""
        self.log.info("Scheduler started")

        # Load config to check if timer polling is enabled
        try:
            config = load_config()
            timer_polling_enabled = config.scheduler_enable_timer_polling()
            self.log.info(f"Config loaded: enable_timer_polling = {timer_polling_enabled}")
        except Exception as e:
            self.log.error(f"Failed to load scheduler config: {e} — defaulting to timer polling enabled")
            timer_polling_enabled = True

        if timer_polling_enabled:
            self.log.info("Timer-based polling ENABLED")
            self._init_schedules()
        else:
            self.log.info("Timer-based polling DISABLED — relying on file watcher only")

        while not self.stop_event.wait(1):  # Check every second
            # Only check timer-based scheduling if enabled
            if timer_polling_enabled:
                now = datetime.fromtimestamp(time.time(), tz=timezone.utc)

                for script, interval in AGENTS:
                    if now >= self.next_run_times[script]:
                        # Schedule next run
                        self.next_run_times[script] = now + timedelta(minutes=interval)

                        # Trigger agent (timer-based, since file-watcher runs in parallel)
                        self.trigger_agent(script, "timer")

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

        # Flush .pyc caches for the scripts directory so agents always import fresh source
        for pycache in (SCRIPTS_DIR).rglob("__pycache__"):
            shutil.rmtree(pycache, ignore_errors=True)
        self.log.info("Flushed __pycache__ directories")

        # Health-check import of shared.task_io — if it fails, log the error and abort
        try:
            spec = importlib.util.spec_from_file_location(
                "task_io_check", SCRIPTS_DIR / "shared" / "task_io.py"
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.log.info("Health check: shared/task_io.py imports cleanly")
        except Exception as e:
            msg = f"FATAL: shared/task_io.py failed to import: {e}. Fix the file before starting agents."
            self.log.error(msg)
            print(msg)
            return  # Abort — do not start agents

        # Initialize file watchers for immediate task detection
        self._init_watchers()

        try:
            self._schedule_agents()
        except KeyboardInterrupt:
            self.log.info("Shutdown requested (Ctrl+C)")
            self.stop_event.set()
            time.sleep(1)  # Give threads time to clean up

            # Stop file watchers if running
            if self.watcher:
                try:
                    self.watcher.stop()
                    self.log.info("File watcher stopped")
                except Exception as e:
                    self.log.error(f"Error stopping file watcher: {e}")

            self.log.info("Scheduler exited cleanly")


def main():
    scheduler = AgentScheduler()
    scheduler.run()


if __name__ == "__main__":
    main()
