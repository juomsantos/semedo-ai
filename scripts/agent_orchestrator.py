"""
agent_orchestrator.py — Orchestrator agent (qwen3:9b).

CRON: */1 * * * * /usr/bin/python3 /path/to/scripts/agent_orchestrator.py

Responsibilities:
  1. Poll inbox/ for pending .task.md files
  2. For each task, call qwen3:9b to:
     a. Decide if task should be decomposed into subtasks
     b. Route each (sub)task to the correct worker agent
  3. Write subtasks to agents/<worker>/inbox/
  4. Move original task to processing/ (then outbox/ when all subtasks complete)
  5. Log all decisions to logs/orchestrator/

Worker agents available:
  - coder       → agents/coder/inbox/        (qwen2.5-coder:7b)
  - research     → agents/research/inbox/     (qwen3:9b)
  - claude-code  → agents/claude-code/inbox/  (claude CLI)

Routing rules (enforced via system prompt, not hardcoded):
  - Code generation, debugging, refactoring → coder
  - Research, summarization, Q&A, writing   → research
  - Complex multi-step reasoning, anything requiring tool use → claude-code
  - Unknown/ambiguous → research (safest fallback)
"""

import sys
import json
import os
import atexit
from pathlib import Path

# Allow importing from shared/ regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.task_io import (
    list_pending_tasks,
    read_task,
    mark_processing,
    mark_completed,
    mark_failed,
    create_task_file,
    PROJECT_ROOT,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.logger import AgentLogger
from shared.config import load_config

AGENT_NAME = "orchestrator"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
INBOX = PROJECT_ROOT / "inbox"
LOCK_FILE = PROJECT_ROOT / "processing" / "orchestrator.lock"


# ---------------------------------------------------------------------------
# Concurrency guard — prevents two orchestrator instances running at once
# ---------------------------------------------------------------------------

def _pid_exists(pid: int) -> bool:
    """Cross-platform check: is this PID still alive?"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def acquire_lock(log: "AgentLogger") -> bool:
    """
    Try to write a lockfile containing our PID.
    Returns True if acquired, False if another instance is already running.
    Stale locks (dead PID) are removed automatically.
    """
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_exists(pid):
                log.info(f"Orchestrator already running (PID {pid}) — skipping this run")
                return False
            else:
                log.warning(f"Removing stale lockfile (PID {pid} no longer running)")
                LOCK_FILE.unlink()
        except Exception:
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile. Registered with atexit so it runs on clean exit or exception."""
    LOCK_FILE.unlink(missing_ok=True)


SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "orchestrator" / "system_prompt.md"

WORKER_INBOXES = {
    "coder": PROJECT_ROOT / "agents" / "coder" / "inbox",
    "research": PROJECT_ROOT / "agents" / "research" / "inbox",
    "claude-code": PROJECT_ROOT / "agents" / "claude-code" / "inbox",
    "pending_approval": PROJECT_ROOT / "agents" / "claude-code" / "pending",
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def parse_routing_decision(response: str) -> list[dict]:
    """
    Parse the LLM's routing decision from its response.
    Expected format is a JSON array of subtask objects:
    [
      {
        "worker": "coder"|"research"|"claude-code",
        "type": "code"|"research"|"summarize"|...,
        "description": "...",
        "expected_output": "..."
      },
      ...
    ]
    TODO: Implement robust parsing with fallback to single-task routing.
    """
    import re

    # Try to extract JSON from markdown code fences (```json ... ```)
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from response: {e}")

    # Ensure it's a list
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    if not data:
        raise ValueError("Routing decision array is empty")

    # Validate each subtask
    valid_workers = {"coder", "research", "claude-code"}
    for i, subtask in enumerate(data):
        if not isinstance(subtask, dict):
            raise ValueError(f"Subtask {i} is not a dict: {type(subtask).__name__}")

        required_fields = {"worker", "type", "description", "expected_output"}
        missing = required_fields - set(subtask.keys())
        if missing:
            raise ValueError(f"Subtask {i} missing required fields: {missing}")

        if subtask["worker"] not in valid_workers:
            raise ValueError(f"Subtask {i} has invalid worker '{subtask['worker']}' (valid: {valid_workers})")

    return data


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    """Route or decompose a single task."""
    task_id = task["meta"].get("id", "unknown")
    log.info(f"Processing task {task_id}")

    # Move to processing immediately so other cron invocations skip it
    task_path = mark_processing(task["path"])
    log.info(f"Moved to processing: {task_path}")

    system_prompt = load_system_prompt()
    # Convert datetime objects to ISO strings for JSON serialization
    meta_for_json = {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in task['meta'].items()}
    user_message = f"---\n{json.dumps(meta_for_json, indent=2)}\n---\n\n{task['body']}"

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message)
        log.info(f"Orchestrator LLM response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error for {task_id}: {e}")
        mark_failed(task_path)
        return

    try:
        subtasks = parse_routing_decision(response)
    except Exception as e:
        log.error(f"Failed to parse routing decision for {task_id}: {e}")
        log.error(f"Raw response: {response[:500]}")
        mark_failed(task_path)
        return

    for subtask in subtasks:
        worker = subtask["worker"]
        
        # Handle pending_approval routing target
        if worker == "pending_approval":
            pending_inbox = PROJECT_ROOT / "agents" / "claude-code" / "pending"
            pending_inbox.mkdir(parents=True, exist_ok=True)
            new_task_path = create_task_file(
                inbox_path=pending_inbox,
                task_type=subtask["type"],
                description=subtask["description"],
                expected_output=subtask["expected_output"],
                assigned_to="pending_approval",
                created_by=AGENT_NAME,
                status="pending_approval",
            )
            log.info(f"Created pending task {new_task_path.name} → pending_approval")
            continue
        
        inbox = WORKER_INBOXES.get(worker)
        if not inbox:
            log.error(f"Unknown worker '{worker}' in routing decision — skipping subtask")
            continue

        # Code tasks should chain to QA for review
        chain_to = "qa" if subtask["type"] == "code" else None
        original_description = subtask["description"] if subtask["type"] == "code" else None

        new_task_path = create_task_file(
            inbox_path=inbox,
            task_type=subtask["type"],
            description=subtask["description"],
            expected_output=subtask["expected_output"],
            assigned_to=worker,
            created_by=AGENT_NAME,
            chain_to=chain_to,
            original_description=original_description,
        )
        log.info(f"Created subtask {new_task_path.name} → {worker}")

    # TODO: Track parent-child task relationships for completion rollup
    # For now, mark original task as completed once subtasks are dispatched
    mark_completed(task_path)
    log.info(f"Task {task_id} dispatched successfully")


def main():
    log = AgentLogger(AGENT_NAME)

    if not acquire_lock(log):
        return  # Another instance is running — exit cleanly
    atexit.register(release_lock)  # Ensure lock is released on any exit

    client = OllamaClient()

    if not client.is_available():
        log.error(f"Ollama is not reachable at {client.base_url} — aborting")
        sys.exit(1)

    tasks = list_pending_tasks(INBOX)
    if not tasks:
        log.info("Inbox empty — nothing to do")
        return

    log.info(f"Found {len(tasks)} pending task(s)")
    for task_path in tasks:
        try:
            task = read_task(task_path)
            process_task(task, client, log)
        except Exception as e:
            log.error(f"Unhandled error processing {task_path.name}: {e}")


if __name__ == "__main__":
    main()
