"""
QA → coder retry-chain discovery helpers.

These functions traverse the filesystem-based task queue to find the latest
terminal QA result for a given coder subtask. They are pure file-IO + frontmatter
parsing — no LLM call, no subprocess.

Idiom: every ``PROJECT_ROOT`` reference is an attribute lookup on
``shared.task_io`` (``_task_io.PROJECT_ROOT``) rather than a name imported at
module load. The ``fake_project`` test fixture monkey-patches
``shared.task_io.PROJECT_ROOT``; resolving the attribute at call time picks up
the patched value automatically.
"""

import logging
from pathlib import Path

import shared.task_io as _task_io
from shared.task_io import read_task

_module_log = logging.getLogger(__name__)

# Sentinel: retry coder task ended up in failed/. Compared with `is` by callers
# (and by tests), so identity must be preserved across re-export.
_RETRY_CODER_FAILED = object()


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
        _task_io.PROJECT_ROOT / "agents" / "qa" / "inbox",
        _task_io.PROJECT_ROOT / "processing",
    ]
    done_dirs = [
        _task_io.PROJECT_ROOT / "validation",
        _task_io.PROJECT_ROOT / "outbox",
        _task_io.PROJECT_ROOT / "failed",
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
        _task_io.PROJECT_ROOT / "agents" / "coder" / "inbox",
        _task_io.PROJECT_ROOT / "processing",
    ]
    done_dirs = [
        _task_io.PROJECT_ROOT / "validation",
        _task_io.PROJECT_ROOT / "outbox",
    ]
    failed_dirs = [
        _task_io.PROJECT_ROOT / "failed",
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
