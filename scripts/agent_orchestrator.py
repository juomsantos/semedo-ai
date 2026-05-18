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

This module is the thin entrypoint. Heavy lifting lives in the ``orchestration/``
package — see ``decompose.py``, ``validate.py``, ``recovery.py``,
``dispatch.py``, ``qa_chain.py``, ``parsing.py``.
"""

import sys
import os
import atexit
from pathlib import Path

# Allow importing from shared/ regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.task_io import (
    list_pending_tasks,
    read_task,
    resolve_task_dependencies,
    PROJECT_ROOT,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.logger import AgentLogger
from shared.config import load_config

AGENT_NAME = "orchestrator"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
OPTIONS = _config.agent_options(AGENT_NAME)
THINKING = _config.agent_thinking(AGENT_NAME)
INBOX = PROJECT_ROOT / "inbox"
LOCK_FILE = PROJECT_ROOT / "processing" / "orchestrator.lock"

SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "orchestrator" / "system_prompt.md"
VALIDATION_PROMPT_PATH = PROJECT_ROOT / "agents" / "orchestrator" / "validation_system_prompt.md"

WORKER_INBOXES = {
    "coder": PROJECT_ROOT / "agents" / "coder" / "inbox",
    "research": PROJECT_ROOT / "agents" / "research" / "inbox",
    "claude-code": PROJECT_ROOT / "agents" / "claude-code" / "inbox",
    "pending_approval": PROJECT_ROOT / "agents" / "claude-code" / "pending",
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def load_validation_prompt() -> str:
    return VALIDATION_PROMPT_PATH.read_text(encoding="utf-8")


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
        except (OSError, ValueError) as e:
            log.warning(f"Removing unreadable lockfile ({type(e).__name__}: {e})")
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile. Registered with atexit so it runs on clean exit or exception."""
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Re-exports: preserve the public surface the test suite reaches into.
#
# tests/test_orchestrator_helpers.py does `import agent_orchestrator as ao`
# and accesses these directly. Object identity for `_RETRY_CODER_FAILED`
# must be preserved because tests use `is`-comparison against it.
# ---------------------------------------------------------------------------

from orchestration.qa_chain import (  # noqa: E402
    _extract_qa_verdict,
    _find_qa_for_coder_subtask,
    _find_qa_for_output,
    _find_retry_coder_output,
    _RETRY_CODER_FAILED,
)

# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

from orchestration.decompose import process_task  # noqa: E402
from orchestration.recovery import (  # noqa: E402
    recover_orphaned_tasks,
    recover_orphaned_validation_subtasks,
    recover_processing_subtasks,
    recover_stalled_subtasks,
)
from orchestration.validate import validation_phase  # noqa: E402


def main():
    log = AgentLogger(AGENT_NAME)

    if not acquire_lock(log):
        return  # Another instance is running — exit cleanly
    atexit.register(release_lock)  # Ensure lock is released on any exit

    recover_orphaned_tasks(log)
    recover_processing_subtasks(log)
    recover_stalled_subtasks(log)
    recover_orphaned_validation_subtasks(log)

    client = OllamaClient()

    if not client.is_available():
        log.error(f"Ollama is not reachable at {client.base_url} — aborting")
        sys.exit(1)

    # Phase 1: Validate completed tasks
    log.info("=== VALIDATION PHASE ===")
    validation_phase(client, log)

    # Phase 2: Resolve pending task dependencies
    agent_inboxes = {
        "coder": PROJECT_ROOT / "agents" / "coder" / "inbox",
        "research": PROJECT_ROOT / "agents" / "research" / "inbox",
        "claude-code": PROJECT_ROOT / "agents" / "claude-code" / "inbox",
        "qa": PROJECT_ROOT / "agents" / "qa" / "inbox",
    }
    log.info("=== DEPENDENCY RESOLUTION PHASE ===")
    resolve_task_dependencies(agent_inboxes)
    log.info("Dependency resolution complete")

    # Phase 3: Decompose and dispatch new tasks
    log.info("=== DISPATCH PHASE ===")
    tasks = list_pending_tasks(INBOX)
    if not tasks:
        log.info("Inbox empty — nothing to do")
        return

    log.info(f"Found {len(tasks)} pending task(s)")
    for task_path in tasks:
        try:
            task = read_task(task_path)
            process_task(task, client, log)
        except OllamaError as e:
            # process_task catches OllamaError internally today, but the outer
            # guard preserves the M3 pattern in case that ever changes.
            log.error(f"Ollama error processing {task_path.name}: {e}")
        except Exception as e:
            log.error(f"Unhandled error processing {task_path.name}: {e}")


if __name__ == "__main__":
    main()
