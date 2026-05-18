"""
Decomposition LLM call.

- ``process_task`` — initial decomposition: takes a freshly-arrived parent task
  from ``inbox/``, calls the orchestrator LLM with the decomposition prompt,
  parses the routing decision, and dispatches subtasks. May set the
  ``redecompose_after_research`` flag on the parent if the LLM wants research
  results before producing a full breakdown.
- ``redecompose_with_research`` — called by the validation phase when a parent
  has the ``redecompose_after_research`` flag set and its research subtasks
  have completed. Re-runs the decomposition LLM with the research outputs in
  context, then dispatches the resulting subtasks.
"""

import json
from pathlib import Path

import shared.task_io as _task_io
from shared.task_io import (
    create_task_file,
    mark_completed,
    mark_failed,
    mark_processing,
    read_task,
    write_result,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.token_logger import log_tokens
from shared.rag_injection import inject_rag_context
from shared.logger import AgentLogger

from orchestration.dispatch import dispatch_subtasks
from orchestration.parsing import parse_routing_decision


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    """Route or decompose a single task."""
    # Late import to avoid a circular import (agent_orchestrator imports from
    # this module to wire up main()).
    from agent_orchestrator import (
        AGENT_NAME,
        MODEL,
        WORKER_INBOXES,
        load_system_prompt,
    )

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

    dispatch_subtasks(
        subtasks,
        parent_task_id=task_id,
        worker_inboxes=WORKER_INBOXES,
        agent_name=AGENT_NAME,
        log=log,
    )

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
    from agent_orchestrator import (
        AGENT_NAME,
        MODEL,
        WORKER_INBOXES,
        load_system_prompt,
    )

    processing_dir = _task_io.PROJECT_ROOT / "processing"
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

    dispatch_subtasks(
        subtasks,
        parent_task_id=parent_task_id,
        worker_inboxes=WORKER_INBOXES,
        agent_name=AGENT_NAME,
        log=log,
        log_prefix="Re-decompose: ",
    )

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
