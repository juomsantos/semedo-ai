"""
Startup recovery passes — clean up state left behind by killed processes,
stalled workers, and parents that completed just before a restart.

Run order (in ``main()``):
  1. ``recover_orphaned_tasks``               — pending parents back to inbox/
  2. ``recover_processing_subtasks``          — stale worker subtasks back to worker inbox/
  3. ``recover_stalled_subtasks``             — failed/ subtasks retried (or parent failed)
  4. ``recover_orphaned_validation_subtasks`` — stranded validation/ subtasks → outbox/
"""

import time
from pathlib import Path

import shared.task_io as _task_io
from shared.task_io import (
    mark_completed,
    mark_failed,
    move_task,
    read_task,
    write_result,
)
from shared.logger import AgentLogger


# Time threshold above which a status:processing subtask is treated as stale and
# returned to its worker inbox. 12 min — comfortably above any realistic LLM
# call including a full tool loop (Ollama timeout ~240s × max tool turns).
STALE_THRESHOLD_SECONDS = 720

# Max stall recoveries (subtask retried from failed/) before the parent is
# itself marked failed.
MAX_STALL_RETRIES = 2


def recover_orphaned_tasks(log: AgentLogger):
    """
    Move any tasks stuck in processing/ with status:pending back to inbox/
    so they can be re-dispatched. These are tasks the orchestrator started
    but never finished decomposing (e.g. killed mid-LLM-call).
    """
    from agent_orchestrator import INBOX

    processing_dir = _task_io.PROJECT_ROOT / "processing"
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
    from agent_orchestrator import WORKER_INBOXES

    processing_dir = _task_io.PROJECT_ROOT / "processing"
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
                f"(age {int(age_seconds)}s, worker={assigned_to}) → {inbox.relative_to(_task_io.PROJECT_ROOT)}"
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
    from agent_orchestrator import WORKER_INBOXES

    failed_dir = _task_io.PROJECT_ROOT / "failed"
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
            processing_dir = _task_io.PROJECT_ROOT / "processing"
            parent_path = processing_dir / f"{parent_task_id}.task.md"

            if not parent_path.exists():
                # If parent is already in outbox (completed), this subtask is simply
                # stale — not a stall.  Skip silently to avoid N10 log noise.
                outbox_parent = _task_io.PROJECT_ROOT / "outbox" / f"{parent_task_id}.task.md"
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
                outbox_dir = _task_io.PROJECT_ROOT / "outbox"
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
    validation_dir = _task_io.PROJECT_ROOT / "validation"
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
                outbox_parent = _task_io.PROJECT_ROOT / "outbox" / f"{parent_task_id}.task.md"
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
