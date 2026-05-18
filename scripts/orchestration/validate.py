"""
Validation phase: review completed subtask results and decide what's next.

- ``validate_completed_tasks`` — calls the orchestrator LLM with the parent
  task + all its completed subtask results and parses the validation decision
  (``complete`` / ``refine`` / ``additional_work`` / ``redo``). Retries the
  LLM call once if the response isn't valid JSON, then gives up with the
  ``_VALIDATION_PARSE_FAILED`` sentinel.
- ``handle_validation_decision`` — routes the decision: marks the parent
  complete + sweeps subtasks to outbox/, or dispatches follow-ups for the
  next iteration.
- ``validation_phase`` — the main loop: groups subtasks by parent, gates on
  in-flight QA, hands off to ``redecompose_with_research`` when flagged.
"""

import json
from pathlib import Path

import shared.task_io as _task_io
from shared.task_io import (
    get_completed_subtasks_by_parent,
    mark_completed,
    mark_failed,
    read_subtask_result,
    read_task,
    write_result,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.token_logger import log_tokens
from shared.logger import AgentLogger

from orchestration.dispatch import dispatch_subtasks
from orchestration.parsing import parse_validation_decision
from orchestration.qa_chain import (
    _extract_qa_verdict,
    _find_qa_for_coder_subtask,
)


# Cap on per-subtask result preview passed to the validation LLM. Above this
# we truncate with a marker note so the LLM doesn't issue follow-up work just
# because the preview was cut off. ~64k tokens at ~4 chars/token.
MAX_RESULT_CHARS = 256000

# Max iterations through the refine/redo/additional_work loop before we force
# a "complete" decision and ship whatever we have.
MAX_ITERATIONS = 5

# Sentinel: validation LLM emitted unparseable JSON twice in a row. Bubbled
# up so the main loop can move the parent to failed/ rather than spin forever.
_VALIDATION_PARSE_FAILED = object()


def validate_completed_tasks(parent_task_id: str, completed_subtasks: list, client: OllamaClient, log: AgentLogger):
    """
    Call orchestrator LLM to validate completed subtasks.
    Returns decision: "complete", "refine", "additional_work", or "redo"
    """
    # Late import to avoid a circular import (agent_orchestrator imports from
    # this module to wire up main()).
    from agent_orchestrator import AGENT_NAME, MODEL, OPTIONS, THINKING, load_validation_prompt

    # Read original parent task from processing/ or outbox/
    processing_dir = _task_io.PROJECT_ROOT / "processing"
    parent_path = None
    for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
        parent_path = candidate
        break

    if not parent_path:
        # Before declaring orphan, check outbox/ — parent may have already completed
        outbox_candidate = _task_io.PROJECT_ROOT / "outbox" / f"{parent_task_id}.task.md"
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

    if iteration >= MAX_ITERATIONS:
        log.warning(f"Task {parent_task_id} reached max iterations ({MAX_ITERATIONS}) — forcing completion")
        return {
            "decision": "complete",
            "reasoning": f"Max iterations ({MAX_ITERATIONS}) reached. Completing task to prevent infinite loop."
        }

    user_message = f"""## Parent Task
ID: {parent_task_id}
Type: {parent_task['meta'].get('type')}
Description:
{parent_task['body']}

## Completed Subtasks (Iteration {iteration}/{MAX_ITERATIONS})
{json.dumps(subtask_results, indent=2)}

Evaluate these results and decide if the work is complete. You have {MAX_ITERATIONS - iteration} iteration(s) remaining."""

    try:
        response = client.chat(model=MODEL, system_prompt=validation_prompt, user_message=user_message, options=OPTIONS, think=THINKING)
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
        repair_response = client.chat(model=MODEL, system_prompt=validation_prompt, user_message=repair_prompt, options=OPTIONS, think=THINKING)
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


def handle_validation_decision(parent_task_id: str, decision: dict, client: OllamaClient, log: AgentLogger):
    """
    Process the validation decision:
    - "complete": Mark parent task as complete
    - "refine"/"additional_work": Create follow-up tasks
    - "redo": Create new subtasks with failure context
    """
    from agent_orchestrator import AGENT_NAME, WORKER_INBOXES

    decision_type = decision.get("decision")
    reasoning = decision.get("reasoning", "No reasoning provided")

    log.info(f"Validation decision for {parent_task_id}: {decision_type}")
    log.info(f"Reasoning: {reasoning}")

    # Get current iteration from parent task
    processing_dir = _task_io.PROJECT_ROOT / "processing"
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

        # Build the validation context payload once — all follow-ups share the same decision
        val_context = {
            "decision_type": decision_type,
            "reasoning": reasoning,
        }

        # For refine/additional_work, collect previous outputs per worker type so they can be
        # wired into context_files for follow-up tasks. This lets each agent actually read
        # what was already produced rather than guessing at it from the description.
        # For redo we intentionally omit them — the agent should start completely fresh.
        prev_research_outputs: list = []
        prev_coder_outputs: list = []
        if decision_type in ("refine", "additional_work"):
            completed = get_completed_subtasks_by_parent(_task_io.PROJECT_ROOT / "validation")
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

        prev_outputs_by_worker = {}
        if prev_research_outputs:
            prev_outputs_by_worker["research"] = prev_research_outputs
        if prev_coder_outputs:
            prev_outputs_by_worker["coder"] = prev_coder_outputs

        dispatch_subtasks(
            follow_ups,
            parent_task_id=parent_task_id,
            worker_inboxes=WORKER_INBOXES,
            agent_name=AGENT_NAME,
            log=log,
            validation_context=val_context,
            prev_outputs_by_worker=prev_outputs_by_worker or None,
            subtask_label="follow-up task",
        )

    if decision_type == "complete":
        # Mark parent task as truly complete (move from processing to outbox)
        processing_dir = _task_io.PROJECT_ROOT / "processing"
        parent_path = None
        for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
            parent_path = candidate
            break

        if parent_path and parent_path.exists():
            # Create a result file with summary and aggregated subtask results
            outbox_dir = _task_io.PROJECT_ROOT / "outbox"
            completed_subtasks = get_completed_subtasks_by_parent(_task_io.PROJECT_ROOT / "validation")
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
            validation_dir = _task_io.PROJECT_ROOT / "validation"
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
    # Late import — decompose imports from agent_orchestrator, which imports
    # validation_phase from this module. Breaking the cycle here.
    from orchestration.decompose import redecompose_with_research

    validation_dir = _task_io.PROJECT_ROOT / "validation"
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
        parent_path = _task_io.PROJECT_ROOT / "processing" / f"{parent_task_id}.task.md"
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
            parent_path = _task_io.PROJECT_ROOT / "processing" / f"{parent_task_id}.task.md"
            if parent_path.exists():
                mark_failed(parent_path, _task_io.PROJECT_ROOT / "failed")
        elif decision:
            handle_validation_decision(parent_task_id, decision, client, log)
