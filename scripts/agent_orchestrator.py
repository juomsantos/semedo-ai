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
import time
import atexit
import logging
from pathlib import Path

# Allow importing from shared/ regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Module-level logger for helper functions that aren't passed the per-task
# AgentLogger. Uses Python's stdlib `logging` so output goes to stderr by default
# but can be wired into the agent's log file if/when needed.
_module_log = logging.getLogger(__name__)

from shared.task_io import (
    list_pending_tasks,
    read_task,
    mark_processing,
    mark_completed,
    mark_failed,
    create_task_file,
    resolve_task_dependencies,
    list_validation_tasks,
    get_completed_subtasks_by_parent,
    write_result,
    move_task,
    read_subtask_result,
    PROJECT_ROOT,
)
from shared.token_logger import log_tokens
from shared.ollama_client import OllamaClient, OllamaError
from shared.rag_injection import inject_rag_context
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
        except (OSError, ValueError) as e:
            log.warning(f"Removing unreadable lockfile ({type(e).__name__}: {e})")
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile. Registered with atexit so it runs on clean exit or exception."""
    LOCK_FILE.unlink(missing_ok=True)


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


def parse_routing_decision(response: str) -> tuple[list[dict], bool]:
    """
    Parse the LLM's routing decision from its response.

    Accepts two formats:

    Plain array (standard decomposition):
    [
      {"worker": "coder", "type": "code", "description": "...", "expected_output": "..."},
      ...
    ]

    Wrapper object (research-first, re-decompose after results):
    {
      "redecompose_after_research": true,
      "subtasks": [
        {"worker": "research", "type": "research", "description": "...", "expected_output": "..."}
      ]
    }

    Returns (subtasks, redecompose_after_research).
    When redecompose_after_research is True all subtasks must target the research worker;
    if non-research subtasks are found the flag is cleared and a warning is logged.
    """
    import re

    # Try to extract JSON from markdown code fences (```json ... ```)
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from response: {e}")

    # Handle wrapper object format
    redecompose_flag = False
    if isinstance(data, dict):
        redecompose_flag = bool(data.get("redecompose_after_research", False))
        data = data.get("subtasks", [])

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    if not data:
        raise ValueError("Routing decision array is empty")

    # Validate each subtask
    valid_workers = {"coder", "research", "claude-code", "pending_approval"}
    for i, subtask in enumerate(data):
        if not isinstance(subtask, dict):
            raise ValueError(f"Subtask {i} is not a dict: {type(subtask).__name__}")

        required_fields = {"worker", "type", "description", "expected_output"}
        missing = required_fields - set(subtask.keys())
        if missing:
            raise ValueError(f"Subtask {i} missing required fields: {missing}")

        if subtask["worker"] not in valid_workers:
            raise ValueError(f"Subtask {i} has invalid worker '{subtask['worker']}' (valid: {valid_workers})")

    # redecompose_after_research is only valid when ALL subtasks target research.
    # If non-research subtasks slipped in, the LLM misused the format — ignore the flag
    # and dispatch normally rather than blocking on a re-decompose that will never resolve.
    if redecompose_flag:
        non_research = [s for s in data if s.get("worker") != "research"]
        if non_research:
            redecompose_flag = False

    return data, redecompose_flag


def _sanitize_json_literals(raw: str) -> str:
    """
    Replace literal newlines / carriage returns / tabs that appear inside JSON
    string values with their escape sequences.  The regex matches JSON string
    literals (including already-escaped sequences via the \\. alternative) so
    it never double-escapes a '\n' that is already escaped.
    """
    import re

    def _fix(m: re.Match) -> str:
        return (
            m.group(0)
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    # "(?:[^"\\]|\\.)*" matches a JSON string including escape sequences.
    # re.DOTALL makes . match newlines so the unescaped-newline case is caught.
    return re.sub(r'"(?:[^"\\]|\\.)*"', _fix, raw, flags=re.DOTALL)


def parse_validation_decision(response: str) -> dict:
    """
    Parse the orchestrator's validation decision.
    Expected format is a JSON object with:
    {
      "decision": "complete|refine|additional_work|redo",
      "reasoning": "...",
      "follow_ups": [...]  # Only if decision != "complete"
    }

    Robustness measures applied before json.loads:
      1. Strip any prose before/after by extracting a ```json ... ``` fence if present.
      2. Sanitize literal newlines inside string values that would make the JSON invalid.
    """
    import re

    # Extract from code fence if present, otherwise use whole response.
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    # Sanitize literal control characters inside string values.
    json_str = _sanitize_json_literals(json_str)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse validation JSON: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    decision = data.get("decision")
    valid_decisions = {"complete", "refine", "additional_work", "redo"}
    if decision not in valid_decisions:
        raise ValueError(f"Invalid decision '{decision}' (valid: {valid_decisions})")

    return data


def _find_qa_for_output(output_path: str):
    """
    Find the QA task whose context_files references the given output_path.

    Returns (status, task_dict):
      "pending"   — QA task exists but is still in-flight
      "done"      — QA task completed (in validation/, outbox/, or failed/)
      "not_found" — no matching QA task found
    """
    output_name = Path(output_path).name

    in_flight_dirs = [
        PROJECT_ROOT / "agents" / "qa" / "inbox",
        PROJECT_ROOT / "processing",
    ]
    done_dirs = [
        PROJECT_ROOT / "validation",
        PROJECT_ROOT / "outbox",
        PROJECT_ROOT / "failed",
    ]

    for folder in in_flight_dirs:
        if not folder.exists():
            continue
        for task_file in folder.glob("*.task.md"):
            try:
                task = read_task(task_file)
                if task["meta"].get("type") != "qa":
                    continue
                if any(Path(cf).name == output_name
                       for cf in task["meta"].get("context_files", [])):
                    return "pending", task
            except (OSError, ValueError, UnicodeDecodeError) as e:
                _module_log.debug(f"Skipping unreadable task file {task_file.name}: {type(e).__name__}: {e}")
                continue

    for folder in done_dirs:
        if not folder.exists():
            continue
        for task_file in folder.glob("*.task.md"):
            try:
                task = read_task(task_file)
                if task["meta"].get("type") != "qa":
                    continue
                if any(Path(cf).name == output_name
                       for cf in task["meta"].get("context_files", [])):
                    return "done", task
            except (OSError, ValueError, UnicodeDecodeError) as e:
                _module_log.debug(f"Skipping unreadable task file {task_file.name}: {type(e).__name__}: {e}")
                continue

    return "not_found", None


def _extract_qa_verdict(qa_task: dict) -> str:
    """Read a QA task's result file and return 'PASS', 'FAIL', or 'UNKNOWN'."""
    output_path = qa_task["meta"].get("output_path", "")
    if not output_path or not Path(output_path).exists():
        return "UNKNOWN"
    content = Path(output_path).read_text(encoding="utf-8")
    for line in content.splitlines():
        upper = line.upper()
        if "PASS" in upper:
            return "PASS"
        if "FAIL" in upper:
            return "FAIL"
    return "UNKNOWN"


_RETRY_CODER_FAILED = object()  # sentinel: retry coder task ended up in failed/
_VALIDATION_PARSE_FAILED = object()  # sentinel: validation LLM response unparseable after retry


def _find_retry_coder_output(qa_task: dict):
    """
    When QA fails (retry_count=0) it dispatches a retry coder task.
    Find that retry coder's output_path if it has completed.

    Returns:
      output_path (str)      — retry coder finished, result file exists
      None                   — retry coder still in-flight (or not yet dispatched)
      _RETRY_CODER_FAILED    — retry coder ended in failed/ (chain exhausted)
    """
    qa_task_id = qa_task["meta"].get("id", "")
    if not qa_task_id:
        return None

    # Only match retry coder tasks created *after* this QA task ran.
    qa_created_at = qa_task["meta"].get("created_at", "")

    # The retry coder task was created_by the qa agent shortly after this qa task ran.
    # It lives in coder/inbox, processing/, validation/, outbox/, or failed/.
    in_flight_dirs = [
        PROJECT_ROOT / "agents" / "coder" / "inbox",
        PROJECT_ROOT / "processing",
    ]
    done_dirs = [
        PROJECT_ROOT / "validation",
        PROJECT_ROOT / "outbox",
    ]
    failed_dirs = [
        PROJECT_ROOT / "failed",
    ]

    def _matches(task: dict) -> bool:
        if task["meta"].get("created_by") != "qa":
            return False
        if task["meta"].get("type") not in ("code",):
            return False
        # Timestamp guard: retry task must have been created at or after the QA task
        candidate_created = task["meta"].get("created_at", "")
        if qa_created_at and candidate_created and candidate_created < qa_created_at:
            return False
        return True

    # Check in-flight first
    for folder in in_flight_dirs:
        if not folder.exists():
            continue
        for task_file in folder.glob("*.task.md"):
            try:
                task = read_task(task_file)
                if _matches(task):
                    return None  # Still running — not ready
            except (OSError, ValueError, UnicodeDecodeError) as e:
                _module_log.debug(f"Skipping unreadable task file {task_file.name}: {type(e).__name__}: {e}")
                continue

    # Check done dirs
    for folder in done_dirs:
        if not folder.exists():
            continue
        for task_file in folder.glob("*.task.md"):
            try:
                task = read_task(task_file)
                if not _matches(task):
                    continue
                output_path = task["meta"].get("output_path", "")
                if output_path and Path(output_path).exists():
                    return output_path
            except (OSError, ValueError, UnicodeDecodeError) as e:
                _module_log.debug(f"Skipping unreadable task file {task_file.name}: {type(e).__name__}: {e}")
                continue

    # Check failed/ — retry coder crashed or timed out; chain is exhausted
    for folder in failed_dirs:
        if not folder.exists():
            continue
        for task_file in folder.glob("*.task.md"):
            try:
                task = read_task(task_file)
                if _matches(task):
                    return _RETRY_CODER_FAILED
            except (OSError, ValueError, UnicodeDecodeError) as e:
                _module_log.debug(f"Skipping unreadable task file {task_file.name}: {type(e).__name__}: {e}")
                continue

    return None


def _find_qa_for_coder_subtask(coder_subtask: dict):
    """
    Follow the QA→coder retry chain starting from a coder subtask to find
    the *latest* terminal QA result.

    The QA agent retries once on its own: FAIL → retry-coder → new-QA.
    The orchestrator should wait for that retry to resolve before acting.

    Returns (status, qa_task_dict):
      "pending"   — QA (or its retry) is still in-flight; wait
      "done"      — latest QA has a terminal result; ready for orchestrator
      "not_found" — no QA task exists yet; wait
      qa_task_dict — the latest QA task found, or None
    """
    coder_output = coder_subtask["meta"].get("output_path", "")
    if not coder_output:
        return "not_found", None

    # Step 1: find QA for the original coder output
    status, qa_task = _find_qa_for_output(coder_output)
    if status in ("not_found", "pending"):
        return status, qa_task

    # QA1 is done — check its verdict
    verdict = _extract_qa_verdict(qa_task)
    retry_count = qa_task["meta"].get("retry_count", 0)

    if verdict == "PASS" or retry_count > 0:
        # Terminal: either passed, or this QA is itself a retry (retry_count>0
        # means QA's own retry loop is exhausted) — orchestrator can act.
        return "done", qa_task

    # verdict == FAIL, retry_count == 0:
    # QA dispatched a retry coder task. Follow the chain.
    retry_output = _find_retry_coder_output(qa_task)
    if retry_output is None:
        # Retry coder is still running (or hasn't started yet)
        return "pending", None
    if retry_output is _RETRY_CODER_FAILED:
        # Retry coder crashed / timed out — chain is exhausted.
        # Return QA1 as the terminal result so the orchestrator can act.
        return "done", qa_task

    # Step 2: find QA for the retry coder output
    status2, qa_task2 = _find_qa_for_output(retry_output)
    if status2 in ("not_found", "pending"):
        return "pending", None  # retry QA not done yet

    # QA2 is done — this is the terminal result the orchestrator should act on
    return "done", qa_task2


def validate_completed_tasks(parent_task_id: str, completed_subtasks: list, client: OllamaClient, log: AgentLogger):
    """
    Call orchestrator LLM to validate completed subtasks.
    Returns decision: "complete", "refine", "additional_work", or "redo"
    """
    # Read original parent task from processing/ or outbox/
    processing_dir = PROJECT_ROOT / "processing"
    parent_path = None
    for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
        parent_path = candidate
        break

    if not parent_path:
        # Before declaring orphan, check outbox/ — parent may have already completed
        outbox_candidate = PROJECT_ROOT / "outbox" / f"{parent_task_id}.task.md"
        if outbox_candidate.exists():
            try:
                parent_meta = read_task(outbox_candidate)["meta"]
            except (OSError, ValueError, UnicodeDecodeError) as e:
                log.warning(f"Could not read outbox parent {outbox_candidate.name}: {type(e).__name__}: {e}")
                parent_meta = {}
            if parent_meta.get("status") == "complete":
                # Parent finished (e.g. force-completed before restart) — subtasks are
                # stale but not failures.  Mark them complete and move to outbox/.
                for subtask in completed_subtasks:
                    try:
                        mark_completed(subtask["path"])
                        log.info(f"Stale subtask {Path(subtask['path']).name} marked complete → outbox/ (parent {parent_task_id} already complete)")
                    except Exception as e:
                        log.error(f"Failed to mark stale subtask complete: {e}")
                return None
        log.error(f"Cannot find parent task {parent_task_id} — moving orphaned subtasks to failed/")
        for subtask in completed_subtasks:
            try:
                mark_failed(subtask["path"])
                log.warning(f"Moved orphaned subtask {Path(subtask['path']).name} to failed/ (parent {parent_task_id} not found)")
            except Exception as e:
                log.error(f"Failed to move orphaned subtask to failed/: {e}")
        return None

    parent_task = read_task(parent_path)

    # Build validation prompt with parent task + completed results
    validation_prompt = load_validation_prompt()

    # Format completed subtasks with their results
    subtask_results = []
    for subtask in completed_subtasks:
        task_id = subtask["meta"].get("id")
        task_type = subtask["meta"].get("type")
        output_path = subtask["meta"].get("output_path")

        # Read the actual result if it exists
        result_content = ""
        if output_path and Path(output_path).exists():
            MAX_RESULT_CHARS = 256000  # ~64k tokens at ~4 chars/token
            raw = Path(output_path).read_text(encoding="utf-8")
            if len(raw) > MAX_RESULT_CHARS:
                result_content = raw[:MAX_RESULT_CHARS] + (
                    f"\n\n[TRUNCATED — showing first {MAX_RESULT_CHARS} of {len(raw)} chars. "
                    f"The full result is present on disk; this preview ends mid-content. "
                    f"Do NOT request more work solely because this preview is cut off.]"
                )
            else:
                result_content = raw

        entry = {
            "task_id": task_id,
            "type": task_type,
            "assigned_to": subtask["meta"].get("assigned_to"),
            "body_preview": subtask["body"][:300],
            "result_preview": result_content,
        }

        # For code subtasks, attach the QA verdict so the LLM has full context.
        # Also enforce the rule: a first-attempt QA FAIL must be a redo, not complete.
        if subtask["meta"].get("chain_to") == "qa" or task_type == "code":
            qa_status, qa_task = _find_qa_for_coder_subtask(subtask)
            if qa_status == "done" and qa_task:
                qa_output_path = qa_task["meta"].get("output_path", "")
                qa_result_content = ""
                if qa_output_path and Path(qa_output_path).exists():
                    qa_result_content = Path(qa_output_path).read_text(encoding="utf-8")
                qa_verdict = _extract_qa_verdict(qa_task)
                qa_retry_count = qa_task["meta"].get("retry_count", 0)
                entry["qa_verdict"] = qa_verdict
                entry["qa_retry_count"] = qa_retry_count
                entry["qa_result_preview"] = qa_result_content[:2000]

                # QA failed on first attempt (retry_count=0): QA has already
                # dispatched its own retry coder task.  _find_qa_for_coder_subtask
                # follows the chain and only returns "done" once QA's retry has
                # also resolved, so reaching this point with retry_count==0 means
                # the chain follower found the retry QA as the terminal result.
                # No extra action needed here — just pass the verdict to the LLM.

        subtask_results.append(entry)

    # Iteration count (for loop prevention)
    iteration = parent_task["meta"].get("iteration", 1)
    max_iterations = 5

    if iteration >= max_iterations:
        log.warning(f"Task {parent_task_id} reached max iterations ({max_iterations}) — forcing completion")
        return {
            "decision": "complete",
            "reasoning": f"Max iterations ({max_iterations}) reached. Completing task to prevent infinite loop."
        }

    user_message = f"""## Parent Task
ID: {parent_task_id}
Type: {parent_task['meta'].get('type')}
Description:
{parent_task['body']}

## Completed Subtasks (Iteration {iteration}/{max_iterations})
{json.dumps(subtask_results, indent=2)}

Evaluate these results and decide if the work is complete. You have {max_iterations - iteration} iteration(s) remaining."""

    try:
        response = client.chat(model=MODEL, system_prompt=validation_prompt, user_message=user_message)
        log_tokens(AGENT_NAME, parent_task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        log.info(f"Validation response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error during validation of {parent_task_id}: {e}")
        return None

    try:
        decision = parse_validation_decision(response)
        return decision
    except Exception as first_err:
        log.warning(
            f"Failed to parse validation decision for {parent_task_id} (attempt 1): {first_err}"
        )
        log.warning(f"Raw response (first 500 chars): {response[:500]}")

    # --- Retry: ask the LLM to emit only valid JSON ---
    repair_prompt = (
        "Your previous response could not be parsed as valid JSON.\n\n"
        "Original response:\n"
        f"{response[:2000]}\n\n"
        "Output ONLY a valid JSON object — no prose, no code fences, no extra fields.\n"
        'Required schema: {"decision": "complete|refine|additional_work|redo", '
        '"reasoning": "...", "follow_ups": [...]}  (follow_ups only when decision != complete)\n'
        "Escape any newlines inside string values as \\n."
    )
    try:
        repair_response = client.chat(model=MODEL, system_prompt=validation_prompt, user_message=repair_prompt)
        log_tokens(AGENT_NAME, parent_task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        log.info(f"Validation repair response received ({len(repair_response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error during validation repair of {parent_task_id}: {e}")
        return _VALIDATION_PARSE_FAILED

    try:
        decision = parse_validation_decision(repair_response)
        log.info(f"Validation repair succeeded for {parent_task_id}")
        return decision
    except Exception as second_err:
        log.error(
            f"Failed to parse validation decision for {parent_task_id} (attempt 2, giving up): {second_err}"
        )
        log.error(f"Raw repair response (first 500 chars): {repair_response[:500]}")
        return _VALIDATION_PARSE_FAILED


def recover_orphaned_tasks(log: AgentLogger):
    """
    Move any tasks stuck in processing/ with status:pending back to inbox/
    so they can be re-dispatched. These are tasks the orchestrator started
    but never finished decomposing (e.g. killed mid-LLM-call).
    """
    processing_dir = PROJECT_ROOT / "processing"
    if not processing_dir.exists():
        return
    for task_file in processing_dir.glob("*.task.md"):
        try:
            task = read_task(task_file)
            if task["meta"].get("status") == "pending":
                dest = INBOX / task_file.name
                task_file.rename(dest)
                log.warning(f"Recovered orphaned task {task_file.name} → inbox/")
        except Exception as e:
            log.error(f"Error inspecting {task_file.name} during recovery: {e}")


def recover_processing_subtasks(log: AgentLogger):
    """
    Recover worker subtasks stuck in processing/ with status:processing.

    This covers the case where a worker process is killed mid-LLM-call (e.g. Ollama
    timeout, OOM, Ctrl+C) after calling mark_processing() but before calling
    mark_awaiting_validation() or mark_failed(). The task is left in processing/ with
    status:processing and no recovery path exists in the other recovery functions
    (recover_orphaned_tasks only handles status:pending; recover_stalled_subtasks only
    handles tasks already in failed/).

    Detection strategy: time-based. The Ollama timeout is 240s; even with the maximum
    number of tool turns the wall-clock ceiling is ~15 min. Any subtask that has been
    status:processing for longer than STALE_THRESHOLD_SECONDS is safely assumed to be
    orphaned — the worker that claimed it is gone.

    Recovery: reset status to pending and return the task to the appropriate worker inbox.
    The existing stall_retry_count mechanism on the parent task is NOT incremented here
    because this is an infrastructure failure (crash), not a task-content failure.
    """
    STALE_THRESHOLD_SECONDS = 720  # 12 min — comfortably above any realistic LLM call

    processing_dir = PROJECT_ROOT / "processing"
    if not processing_dir.exists():
        return

    now = time.time()

    for task_file in processing_dir.glob("*.task.md"):
        # Skip the orchestrator lockfile
        if task_file.name == "orchestrator.lock":
            continue
        try:
            task = read_task(task_file)
            meta = task["meta"]

            if meta.get("status") != "processing":
                continue

            assigned_to = meta.get("assigned_to", "")
            if assigned_to == "orchestrator":
                continue  # Orchestrator stalls handled separately via its own lock

            inbox = WORKER_INBOXES.get(assigned_to)
            if not inbox:
                log.warning(f"Stuck processing subtask {task_file.name} has unknown worker '{assigned_to}' — skipping")
                continue

            age_seconds = now - task_file.stat().st_mtime
            if age_seconds < STALE_THRESHOLD_SECONDS:
                continue  # Still within normal processing window

            # Task is stale — reset and return to worker inbox
            meta["status"] = "pending"
            write_result(str(task_file), task["body"], meta=meta)
            move_task(task_file, inbox)
            log.warning(
                f"Recovered stale processing subtask {task_file.name} "
                f"(age {int(age_seconds)}s, worker={assigned_to}) → {inbox.relative_to(PROJECT_ROOT)}"
            )

        except Exception as e:
            log.error(f"Error inspecting {task_file.name} during processing recovery: {e}")


def recover_stalled_subtasks(log: AgentLogger):
    """
    Recover failed subtasks from Ollama timeouts.

    When a worker hits the 240s Ollama timeout, it calls mark_failed() which moves
    the subtask to failed/. The orchestrator's validation loop only monitors validation/,
    so the parent task never gets notified of the failure and sits in processing/ forever.

    This function:
    1. Scans failed/ for .task.md files (subtask files, not QA failure reports)
    2. For each failed subtask, checks if its parent is stalled in processing/
    3. If parent exists and hasn't exceeded max retries, retries the subtask
    4. If parent has exhausted retries, fails the entire parent task
    """
    MAX_STALL_RETRIES = 2

    failed_dir = PROJECT_ROOT / "failed"
    if not failed_dir.exists():
        return

    # Collect all failed subtasks grouped by parent
    failed_subtasks_by_parent = {}

    for subtask_file in failed_dir.glob("*.task.md"):
        try:
            subtask = read_task(subtask_file)
            parent_task_id = subtask["meta"].get("parent_task_id")

            if not parent_task_id:
                log.warning(f"Failed subtask {subtask_file.name} has no parent_task_id — skipping")
                continue

            # Check if parent exists in processing/
            processing_dir = PROJECT_ROOT / "processing"
            parent_path = processing_dir / f"{parent_task_id}.task.md"

            if not parent_path.exists():
                # If parent is already in outbox (completed), this subtask is simply
                # stale — not a stall.  Skip silently to avoid N10 log noise.
                outbox_parent = PROJECT_ROOT / "outbox" / f"{parent_task_id}.task.md"
                if not outbox_parent.exists():
                    log.debug(f"Parent {parent_task_id} not found anywhere for failed subtask {subtask_file.name} — skipping")
                continue

            # Group by parent
            if parent_task_id not in failed_subtasks_by_parent:
                failed_subtasks_by_parent[parent_task_id] = {
                    "parent_path": parent_path,
                    "subtasks": []
                }
            failed_subtasks_by_parent[parent_task_id]["subtasks"].append({
                "file": subtask_file,
                "data": subtask
            })
        except Exception as e:
            log.error(f"Error reading failed subtask {subtask_file.name}: {e}")

    # Process each parent group
    for parent_task_id, group in failed_subtasks_by_parent.items():
        parent_path = group["parent_path"]
        failed_subtasks = group["subtasks"]

        try:
            parent_task = read_task(parent_path)
            stall_retry_count = parent_task["meta"].get("stall_retry_count", 0)

            if stall_retry_count >= MAX_STALL_RETRIES:
                # Max retries exhausted — fail the parent task
                log.warning(f"Stall recovery: parent {parent_task_id} exhausted {MAX_STALL_RETRIES} stall retries — marking failed")

                # Write failure report
                failure_report = f"""# Stall Recovery — Max Retries Exhausted

Task {parent_task_id} had the following subtasks fail with Ollama timeouts:

"""
                for subtask_info in failed_subtasks:
                    subtask_id = subtask_info["data"]["meta"].get("id", "unknown")
                    failure_report += f"- {subtask_id}\n"

                failure_report += f"""

After {MAX_STALL_RETRIES} retries, the subtasks continue to fail. The parent task is being marked as failed.
"""
                outbox_dir = PROJECT_ROOT / "outbox"
                outbox_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(outbox_dir / f"{parent_task_id}_result.md")
                write_result(output_path, failure_report, meta={"task_id": parent_task_id, "status": "failed"})

                # Move parent to failed/
                mark_failed(parent_path)
                log.info(f"Parent task {parent_task_id} moved to failed/")

            else:
                # Retry the subtasks
                for subtask_info in failed_subtasks:
                    subtask_file = subtask_info["file"]
                    subtask_data = subtask_info["data"]

                    # Reset subtask status to pending
                    subtask_data["meta"]["status"] = "pending"
                    write_result(str(subtask_file), subtask_data["body"], meta=subtask_data["meta"])

                    # Get worker inbox
                    assigned_to = subtask_data["meta"].get("assigned_to")
                    inbox_path = WORKER_INBOXES.get(assigned_to)

                    if not inbox_path:
                        log.error(f"Unknown worker '{assigned_to}' for subtask {subtask_file.name}")
                        continue

                    # Move to worker inbox
                    move_task(subtask_file, inbox_path)
                    log.info(f"Stall recovery: retrying subtask {subtask_file.name} → {assigned_to} (attempt {stall_retry_count + 1}/{MAX_STALL_RETRIES})")

                # Increment stall_retry_count on parent (only once per group, not per subtask)
                parent_task["meta"]["stall_retry_count"] = stall_retry_count + 1
                write_result(str(parent_path), parent_task["body"], meta=parent_task["meta"])
                log.info(f"Parent {parent_task_id} stall_retry_count incremented to {stall_retry_count + 1}")

        except Exception as e:
            log.error(f"Error processing stalled parent {parent_task_id}: {e}")


def recover_orphaned_validation_subtasks(log: AgentLogger):
    """
    Sweep orphaned subtasks from validation/ to outbox/.

    A subtask stuck in validation/ is orphaned when its parent has already
    completed and moved to outbox/, but the orchestrator's normal validation
    phase never cleaned it up.  This happens for two categories:

    1. Subtasks WITH parent_task_id whose parent is complete in outbox/
       (e.g. coder subtasks whose parent finished via another iteration).
    2. Subtasks WITHOUT parent_task_id (QA tasks, QA-dispatched retry coders)
       whose result file already exists in outbox/ — meaning a worker finished
       the task but the orchestrator never called mark_completed() on the file.

    Note: timed-out tasks with no result and no parent_task_id are not handled
    here; they remain in validation/ until manual cleanup.
    """
    validation_dir = PROJECT_ROOT / "validation"
    if not validation_dir.exists():
        return

    for task_file in validation_dir.glob("*.task.md"):
        try:
            task = read_task(task_file)
            meta = task["meta"]
            orphaned = False

            # Case 1: has parent_task_id and parent is complete in outbox/
            parent_task_id = meta.get("parent_task_id")
            if parent_task_id:
                outbox_parent = PROJECT_ROOT / "outbox" / f"{parent_task_id}.task.md"
                if outbox_parent.exists():
                    try:
                        parent_meta = read_task(outbox_parent)["meta"]
                        if parent_meta.get("status") == "complete":
                            orphaned = True
                    except (OSError, ValueError, UnicodeDecodeError) as e:
                        log.debug(f"Could not read parent {outbox_parent.name} during orphan recovery: {type(e).__name__}: {e}")

            # Case 2: no parent_task_id but result file already written to outbox/
            # (covers QA tasks and QA-dispatched retry coders that completed but
            # were never swept by the normal validation loop)
            # Guard: only applies when there is no parent_task_id — subtasks with
            # a live parent must go through the normal validation loop even if
            # their result file already exists in outbox/.
            if not orphaned and not parent_task_id:
                output_path = meta.get("output_path")
                if output_path and Path(output_path).exists():
                    orphaned = True

            if orphaned:
                mark_completed(str(task_file))
                log.warning(
                    f"Swept orphaned validation subtask {task_file.name} → outbox/"
                )
        except Exception as e:
            log.error(
                f"Error sweeping orphaned validation subtask {task_file.name}: {e}"
            )


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
    # Pre-prompt RAG injection — the orchestrator's decomposition LLM call
    # has no tool loop, so this is the only way it can consult the knowledge
    # base. See shared/rag_injection.py.
    task_body = inject_rag_context(task['body'])

    user_message = f"---\n{json.dumps(meta_for_json, indent=2)}\n---\n\n{task_body}"

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message)
        log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        log.info(f"Orchestrator LLM response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error for {task_id}: {e}")
        mark_failed(task_path)
        return

    try:
        subtasks, redecompose_after_research = parse_routing_decision(response)
    except Exception as e:
        log.error(f"Failed to parse routing decision for {task_id}: {e}")
        log.error(f"Raw response: {response[:500]}")
        mark_failed(task_path)
        return

    # Track created subtasks to build dependency graph
    created_subtasks = {}

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
                parent_task_id=task_id,
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
            parent_task_id=task_id,
        )

        # Track subtask for dependency wiring
        created_subtasks[worker] = new_task_path
        log.info(f"Created subtask {new_task_path.name} → {worker}")

    # Wire dependencies: if both research and code tasks exist, code depends on research
    if "research" in created_subtasks and "coder" in created_subtasks:
        research_path = created_subtasks["research"]
        coder_path = created_subtasks["coder"]
        research_task = read_task(research_path)

        # Read the coder task and add research output as dependency
        coder_task = read_task(coder_path)
        coder_task["meta"]["depends_on"] = [research_task["meta"]["id"]]

        # Re-write coder task with dependency info
        coder_body = coder_task["body"]
        write_result(str(coder_path), coder_body, meta=coder_task["meta"])
        log.info(f"Wired dependency: coder {coder_path.name} depends on research {research_path.name}")

    # Update parent task status to "dispatched" so orphan recovery does not
    # re-dispatch it on the next cycle (recover_orphaned_tasks checks status=="pending").
    # Also persist the redecompose_after_research flag when set so validation_phase
    # can detect it and re-run decomposition once research results arrive.
    try:
        parent_task = read_task(task_path)
        parent_task["meta"]["status"] = "dispatched"
        if redecompose_after_research:
            parent_task["meta"]["redecompose_after_research"] = True
            log.info(f"Task {task_id} flagged for re-decomposition after research completes")
        write_result(str(task_path), parent_task["body"], meta=parent_task["meta"])
        log.info(f"Parent task {task_id} status updated to 'dispatched'")
    except Exception as e:
        log.error(f"Failed to update parent task status for {task_id}: {e}")

    log.info(f"Task {task_id} dispatched — awaiting subtask completion in validation loop")


def redecompose_with_research(parent_task_id: str, completed_subtasks: list, client: OllamaClient, log: AgentLogger):
    """
    Re-run the decomposition prompt for a parent task that was flagged with
    redecompose_after_research=True, now that its research subtasks have completed.

    Steps:
      1. Collect all research outputs from completed_subtasks
      2. Call the decomposition LLM with the original task + research results as context
      3. Dispatch the resulting subtasks (wire dependencies as normal)
      4. Mark the completed research subtasks as done → outbox/
      5. Remove the flag from the parent, increment iteration, keep status=dispatched
    """
    processing_dir = PROJECT_ROOT / "processing"
    parent_path = processing_dir / f"{parent_task_id}.task.md"

    if not parent_path.exists():
        log.error(f"redecompose_with_research: parent {parent_task_id} not found in processing/")
        return

    parent_task = read_task(parent_path)

    # Collect research outputs
    research_sections = []
    for subtask in completed_subtasks:
        if subtask["meta"].get("type") == "research":
            output_path = subtask["meta"].get("output_path", "")
            if output_path and Path(output_path).exists():
                content = Path(output_path).read_text(encoding="utf-8")
                research_sections.append(
                    f"### Research Result ({Path(output_path).stem})\n\n{content}"
                )

    if not research_sections:
        log.warning(f"redecompose_with_research: no research outputs found for {parent_task_id} — falling back to normal validation")
        return

    # Build the decomposition prompt user message: original task + research context
    system_prompt = load_system_prompt()
    meta_for_json = {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in parent_task["meta"].items()}
    research_block = "\n\n---\n\n".join(research_sections)
    user_message = (
        f"---\n{json.dumps(meta_for_json, indent=2)}\n---\n\n"
        f"{parent_task['body']}\n\n"
        f"## Research Results\n\n"
        f"The following research was completed to inform your decomposition decision. "
        f"Use it to produce a fully-informed subtask breakdown.\n\n"
        f"{research_block}"
    )

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message)
        log_tokens(AGENT_NAME, parent_task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        log.info(f"Re-decomposition response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error during re-decomposition of {parent_task_id}: {e}")
        return

    try:
        subtasks, redecompose_again = parse_routing_decision(response)
        if redecompose_again:
            # Guard against infinite loop: the LLM returned the flag again on a re-decompose
            # call. Ignore the flag and dispatch whatever subtasks it produced.
            log.warning(
                f"redecompose_with_research: {parent_task_id} returned redecompose_after_research "
                f"again — ignoring flag to prevent infinite loop, dispatching subtasks as-is"
            )
    except Exception as e:
        log.error(f"Failed to parse re-decomposition decision for {parent_task_id}: {e}")
        log.error(f"Raw response: {response[:500]}")
        return

    # Dispatch new subtasks (mirrors process_task dispatch logic)
    created_subtasks = {}
    for subtask in subtasks:
        worker = subtask["worker"]

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
                parent_task_id=parent_task_id,
            )
            log.info(f"Re-decompose: created pending task {new_task_path.name} → pending_approval")
            continue

        inbox = WORKER_INBOXES.get(worker)
        if not inbox:
            log.error(f"Re-decompose: unknown worker '{worker}' — skipping subtask")
            continue

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
            parent_task_id=parent_task_id,
        )
        created_subtasks[worker] = new_task_path
        log.info(f"Re-decompose: created subtask {new_task_path.name} → {worker}")

    # Wire research→coder dependency if both present
    if "research" in created_subtasks and "coder" in created_subtasks:
        research_path = created_subtasks["research"]
        coder_path = created_subtasks["coder"]
        research_task_data = read_task(research_path)
        coder_task_data = read_task(coder_path)
        coder_task_data["meta"]["depends_on"] = [research_task_data["meta"]["id"]]
        write_result(str(coder_path), coder_task_data["body"], meta=coder_task_data["meta"])
        log.info(f"Re-decompose: wired coder dependency on research")

    # Mark the completed research subtasks as done so they move to outbox/
    for subtask in completed_subtasks:
        try:
            mark_completed(subtask["path"])
            log.info(f"Re-decompose: marked research subtask {Path(subtask['path']).name} complete → outbox/")
        except Exception as e:
            log.error(f"Re-decompose: failed to mark research subtask complete: {e}")

    # Update parent: remove flag, increment iteration, keep dispatched
    current_iter = parent_task["meta"].get("iteration", 1)
    parent_task["meta"].pop("redecompose_after_research", None)
    parent_task["meta"]["iteration"] = current_iter + 1
    parent_task["meta"]["status"] = "dispatched"
    write_result(str(parent_path), parent_task["body"], meta=parent_task["meta"])
    log.info(f"Task {parent_task_id} re-decomposed — iteration → {current_iter + 1}, awaiting new subtasks")


def handle_validation_decision(parent_task_id: str, decision: dict, client: OllamaClient, log: AgentLogger):
    """
    Process the validation decision:
    - "complete": Mark parent task as complete
    - "refine"/"additional_work": Create follow-up tasks
    - "redo": Create new subtasks with failure context
    """
    decision_type = decision.get("decision")
    reasoning = decision.get("reasoning", "No reasoning provided")

    log.info(f"Validation decision for {parent_task_id}: {decision_type}")
    log.info(f"Reasoning: {reasoning}")

    # Get current iteration from parent task
    processing_dir = PROJECT_ROOT / "processing"
    parent_path = None
    for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
        parent_path = candidate
        break

    current_iteration = 1
    if parent_path:
        parent_task = read_task(parent_path)
        current_iteration = parent_task["meta"].get("iteration", 1)

    # Handle follow-up task creation if needed
    follow_ups = decision.get("follow_ups", [])
    if follow_ups:
        log.info(f"Creating {len(follow_ups)} follow-up task(s) for iteration {current_iteration + 1}")
        created_follow_ups = {}  # track by worker for dependency wiring

        # Build the validation context payload once — all follow-ups share the same decision
        val_context = {
            "decision_type": decision_type,
            "reasoning": reasoning,
        }

        # For refine/additional_work, collect previous outputs per worker type so they can be
        # wired into context_files for follow-up tasks. This lets each agent actually read
        # what was already produced rather than guessing at it from the description.
        # For redo we intentionally omit them — the agent should start completely fresh.
        prev_research_outputs = []
        prev_coder_outputs = []
        if decision_type in ("refine", "additional_work"):
            completed = get_completed_subtasks_by_parent(PROJECT_ROOT / "validation")
            for subtask in completed.get(parent_task_id, []):
                subtask_type = subtask["meta"].get("type")
                output_path = subtask["meta"].get("output_path", "")
                if not output_path or not Path(output_path).exists():
                    continue
                if subtask_type == "research":
                    prev_research_outputs.append(output_path)
                elif subtask_type == "code":
                    prev_coder_outputs.append(output_path)
            if prev_research_outputs:
                log.info(f"Wiring {len(prev_research_outputs)} previous research output(s) into research follow-up context")
            if prev_coder_outputs:
                log.info(f"Wiring {len(prev_coder_outputs)} previous coder output(s) into coder follow-up context")

        for idx, followup in enumerate(follow_ups):
            worker = followup.get("worker")
            inbox = WORKER_INBOXES.get(worker)
            if not inbox:
                log.error(f"Unknown worker '{worker}' in follow-up — skipping")
                continue

            chain_to = "qa" if followup.get("type") == "code" else None
            # For code follow-ups, set original_description to the clean task description.
            # Without this, agent_coder falls back to task["body"] when creating the QA task,
            # which already contains the ## Validation Context block — causing it to be
            # duplicated and nested inside ## Task Description in the QA task body.
            original_description = followup.get("description") if followup.get("type") == "code" else None
            # Pass previous outputs as context so agents can build on prior work rather than
            # starting blind. Research follow-ups get previous research; coder follow-ups get
            # previous coder outputs (so both the coder and, via agent_coder's QA chaining,
            # QA can see the full accumulated codebase, not just the latest delta).
            if worker == "research" and prev_research_outputs:
                followup_context_files = prev_research_outputs
            elif worker == "coder" and prev_coder_outputs:
                followup_context_files = prev_coder_outputs
            else:
                followup_context_files = None
            new_task_path = create_task_file(
                inbox_path=inbox,
                task_type=followup.get("type"),
                description=followup.get("description"),
                expected_output=followup.get("expected_output"),
                assigned_to=worker,
                created_by=AGENT_NAME,
                parent_task_id=parent_task_id,
                chain_to=chain_to,
                original_description=original_description,
                context_files=followup_context_files,
                validation_context=val_context,
            )
            created_follow_ups[worker] = new_task_path
            log.info(f"Created follow-up task {new_task_path.name} → {worker}")

        # Wire depends_on: if both research and coder follow-ups exist, coder must wait for research
        if "research" in created_follow_ups and "coder" in created_follow_ups:
            research_path = created_follow_ups["research"]
            coder_path = created_follow_ups["coder"]
            research_task = read_task(research_path)
            coder_task = read_task(coder_path)
            coder_task["meta"]["depends_on"] = [research_task["meta"]["id"]]
            write_result(str(coder_path), coder_task["body"], meta=coder_task["meta"])
            log.info(f"Wired follow-up dependency: coder {coder_path.name} depends on research {research_path.name}")

    if decision_type == "complete":
        # Mark parent task as truly complete (move from processing to outbox)
        processing_dir = PROJECT_ROOT / "processing"
        parent_path = None
        for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
            parent_path = candidate
            break

        if parent_path and parent_path.exists():
            # Create a result file with summary and aggregated subtask results
            outbox_dir = PROJECT_ROOT / "outbox"
            completed_subtasks = get_completed_subtasks_by_parent(PROJECT_ROOT / "validation")
            subtasks_for_parent = completed_subtasks.get(parent_task_id, [])

            result_content = f"# Task Completion Summary\n\nTask {parent_task_id} completed after validation.\n\n## Decision Reasoning\n\n{reasoning}"

            # Add aggregated subtask results
            if subtasks_for_parent:
                result_content += "\n\n## Subtask Results\n"
                # Group subtasks by type for better organization
                subtasks_by_type = {}
                for subtask in subtasks_for_parent:
                    task_type = subtask["meta"].get("type", "unknown")
                    if task_type not in subtasks_by_type:
                        subtasks_by_type[task_type] = []
                    subtasks_by_type[task_type].append(subtask)

                # Preferred order: research, code, qa, then others
                type_order = ["research", "code", "qa"]
                for task_type in type_order:
                    if task_type in subtasks_by_type:
                        for subtask in subtasks_by_type[task_type]:
                            task_id = subtask["meta"].get("id", "unknown")
                            output_path = subtask["meta"].get("output_path")
                            result_content += f"\n### {task_type.capitalize()} Result (Task: {task_id})\n\n"
                            if output_path:
                                subtask_content = read_subtask_result(output_path)
                                result_content += subtask_content
                            else:
                                result_content += "[No output path recorded for this subtask]"

                # Include other task types not in the preferred order
                for task_type, subtasks_list in subtasks_by_type.items():
                    if task_type not in type_order:
                        for subtask in subtasks_list:
                            task_id = subtask["meta"].get("id", "unknown")
                            output_path = subtask["meta"].get("output_path")
                            result_content += f"\n### {task_type.capitalize()} Result (Task: {task_id})\n\n"
                            if output_path:
                                subtask_content = read_subtask_result(output_path)
                                result_content += subtask_content
                            else:
                                result_content += "[No output path recorded for this subtask]"

            output_path = str(outbox_dir / f"{parent_task_id}_result.md")
            write_result(output_path, result_content, meta={"task_id": parent_task_id, "status": "complete"})

            mark_completed(parent_path)
            log.info(f"Task {parent_task_id} APPROVED and marked complete")

            # Mark all known subtasks complete (those with parent_task_id set).
            for subtask in subtasks_for_parent:
                try:
                    mark_completed(subtask["path"])
                    log.info(f"Subtask {Path(subtask['path']).name} marked complete → outbox/")
                except Exception as e:
                    log.error(f"Failed to mark subtask complete: {e}")

            # Belt-and-suspenders: sweep any remaining tasks in validation/ for this
            # parent that were missed above (e.g. QA tasks or retry coders created
            # before parent_task_id stamping was in place).
            validation_dir = PROJECT_ROOT / "validation"
            for leftover in validation_dir.glob("*.task.md"):
                try:
                    leftover_meta = read_task(leftover)["meta"]
                    if leftover_meta.get("parent_task_id") == parent_task_id:
                        mark_completed(str(leftover))
                        log.info(f"Swept leftover validation subtask {leftover.name} → outbox/")
                except Exception as e:
                    log.error(f"Failed to sweep leftover subtask {leftover.name}: {e}")

    elif decision_type in ["refine", "additional_work", "redo"]:
        # Follow-ups have been created; update parent's iteration counter and keep in processing
        if parent_path and parent_path.exists():
            parent_task = read_task(parent_path)
            current_iter = parent_task["meta"].get("iteration", 1)
            parent_task["meta"]["iteration"] = current_iter + 1
            parent_task["meta"]["last_validation"] = decision_type
            # Update the parent task file with new iteration
            write_result(str(parent_path), parent_task["body"], meta=parent_task["meta"])
            log.info(f"Task {parent_task_id} iteration incremented to {current_iter + 1}")

        log.info(f"Task {parent_task_id} needs more work — follow-ups created, awaiting next iteration")


def validation_phase(client: OllamaClient, log: AgentLogger):
    """
    Validate all completed subtasks waiting in the validation folder.
    Group by parent task and call orchestrator LLM to decide: complete or create follow-ups.
    """
    validation_dir = PROJECT_ROOT / "validation"
    if not validation_dir.exists():
        return

    grouped = get_completed_subtasks_by_parent(validation_dir)
    if not grouped:
        log.info("No tasks awaiting validation")
        return

    log.info(f"Found {len(grouped)} parent task(s) with completed subtasks awaiting validation")

    for parent_task_id, completed_subtasks in grouped.items():
        log.info(f"Validating {len(completed_subtasks)} subtask(s) for parent {parent_task_id}")

        # Check for research-first re-decomposition before anything else.
        # If the parent was flagged with redecompose_after_research, skip normal validation
        # and re-run the decomposition prompt with the research outputs in context.
        parent_path = PROJECT_ROOT / "processing" / f"{parent_task_id}.task.md"
        if parent_path.exists():
            try:
                parent_meta = read_task(parent_path)["meta"]
                if parent_meta.get("redecompose_after_research"):
                    log.info(f"Parent {parent_task_id} has redecompose_after_research flag — re-decomposing")
                    redecompose_with_research(parent_task_id, completed_subtasks, client, log)
                    continue
            except Exception as e:
                log.error(f"Failed to read parent task {parent_task_id} for redecompose check: {e}")

        # Gate: if any code subtask is still waiting for QA, skip this parent for now.
        qa_still_running = False
        for subtask in completed_subtasks:
            if subtask["meta"].get("chain_to") == "qa" or subtask["meta"].get("type") == "code":
                qa_status, _ = _find_qa_for_coder_subtask(subtask)
                if qa_status in ("pending", "not_found"):
                    log.info(
                        f"Skipping validation for parent {parent_task_id} — "
                        f"QA not yet complete for coder subtask {subtask['meta'].get('id')} "
                        f"(qa_status={qa_status})"
                    )
                    qa_still_running = True
                    break
        if qa_still_running:
            continue

        decision = validate_completed_tasks(parent_task_id, completed_subtasks, client, log)
        if decision is _VALIDATION_PARSE_FAILED:
            log.error(
                f"Validation LLM returned unparseable JSON twice for {parent_task_id} — failing task"
            )
            parent_path = PROJECT_ROOT / "processing" / f"{parent_task_id}.task.md"
            if parent_path.exists():
                mark_failed(parent_path, PROJECT_ROOT / "failed")
        elif decision:
            handle_validation_decision(parent_task_id, decision, client, log)


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
        except Exception as e:
            log.error(f"Unhandled error processing {task_path.name}: {e}")


if __name__ == "__main__":
    main()
