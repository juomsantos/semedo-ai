"""
logger.py — Simple file-based logger for agent scripts.

Writes append-only logs to logs/<agent_name>/<task_id>.log
and also prints to stdout for cron visibility.

Usage:
    from shared.logger import AgentLogger

    log = AgentLogger("orchestrator", task_id="task_20260505_001")
    log.info("Routing task to coder agent")
    log.error("Failed to parse Ollama response")
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
import io

# Ensure stdout uses UTF-8 encoding on all platforms
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AgentLogger:
    def __init__(self, agent_name: str, task_id: str = "general"):
        self.agent_name = agent_name
        self.task_id = task_id
        self.log_dir = PROJECT_ROOT / "logs" / agent_name
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"{task_id}.log"

    def _write(self, level: str, message: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] [{level}] [{self.agent_name}] {message}"
        print(line, file=sys.stdout)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def info(self, message: str):
        self._write("INFO", message)

    def warning(self, message: str):
        self._write("WARN", message)

    def error(self, message: str):
        self._write("ERROR", message)

    def debug(self, message: str):
        self._write("DEBUG", message)
