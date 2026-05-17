"""
token_logger.py — Append per-call token usage to logs/<agent>/tokens.jsonl.

Each line: {"ts": "<UTC ISO>", "task_id": "<id>", "prompt": N, "completion": N}
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from shared.task_io import PROJECT_ROOT

# Only log entries for real task IDs (format: task_YYYYMMDD_HHMMSS_microseconds).
# This prevents test/dev data from polluting the token log and the dashboard stats.
_TASK_ID_RE = re.compile(r"^task_\d{8}_\d{6}_\d+$")


def log_tokens(agent_name: str, task_id: str, prompt_tokens: int, completion_tokens: int) -> None:
    """
    Log token usage for an LLM call to logs/<agent>/tokens.jsonl.

    Args:
        agent_name: Name of the agent (e.g., "coder", "research")
        task_id: Task ID being processed
        prompt_tokens: Number of prompt tokens consumed
        completion_tokens: Number of completion tokens generated
    """
    if not _TASK_ID_RE.match(task_id):
        # Skip test/dev entries that don't match the production task ID format.
        return

    log_dir = PROJECT_ROOT / "logs" / agent_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tokens.jsonl"

    entry = {
        "ts": datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id": task_id,
        "prompt": prompt_tokens,
        "completion": completion_tokens,
    }

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
