"""
agent_coder.py — Coder worker agent.

Invocation: run by scripts/scheduler.py — triggered immediately by the
agents/coder/inbox/ file watcher (and on the scheduler's periodic interval if
timer polling is enabled; it is disabled by default). The model is read from
config.json (agents.coder.model).

Responsibilities:
  1. Poll agents/coder/inbox/ for pending .task.md files
  2. Call the coder model with the task + system prompt
  3. Write the generated code to the task's output_path
  4. Move task to outbox/ on success, failed/ on error
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.task_io import (
    list_pending_tasks,
    read_task,
    write_result,
    mark_processing,
    mark_awaiting_validation,
    mark_failed,
    create_task_file,
    PROJECT_ROOT,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.agent_boilerplate import build_user_message, load_system_prompt, log_tokens_safe
from shared.logger import AgentLogger
from shared.config import load_config
# Read-only queue lookup used to make QA chaining idempotent (no duplicate QA
# task if this coder task is re-run by crash recovery). qa_chain imports only
# shared.* so there is no circular import.
from orchestration.qa_chain import _find_qa_for_output

AGENT_NAME = "coder"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
OPTIONS = _config.agent_options(AGENT_NAME)
THINKING = _config.agent_thinking(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "coder" / "inbox"


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    task_id = task["meta"].get("id", "unknown")

    # Skip tasks with unresolved dependencies
    if task["meta"].get("depends_on"):
        log.info(f"Skipping task {task_id} — unresolved dependencies: {task['meta']['depends_on']}")
        return

    log.info(f"Processing task {task_id}")

    task_path = mark_processing(task["path"])

    system_prompt = load_system_prompt(AGENT_NAME)
    # Pre-prompt RAG injection — the coder has no tool loop, so this is the
    # only way it can consult the knowledge base. See shared/rag_injection.py.
    user_message = build_user_message(task, style="coder", use_rag=True, logger=log)

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message, options=OPTIONS, think=THINKING)
        log_tokens_safe(AGENT_NAME, task_id, client)
        log.info(f"Coder response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error for {task_id}: {e}")
        mark_failed(task_path)
        return

    output_path = task["meta"].get("output_path")
    if not output_path:
        output_path = str(PROJECT_ROOT / "outbox" / f"{task_id}_result.md")

    write_result(output_path, response, meta={"task_id": task_id, "agent": AGENT_NAME, "model": MODEL})

    chain_to = task["meta"].get("chain_to")
    if chain_to == "qa":
        # Forward validation_context so QA knows whether it is reviewing a redo/refine/
        # additional_work attempt and can calibrate its review accordingly.
        # The orchestrator's reasoning (what previously failed) is embedded in this dict.
        qa_validation_context = task["meta"].get("validation_context")

        # Include any context files the coder task itself received (e.g. previous coder
        # iterations wired in by the orchestrator for refine/additional_work tasks) so
        # QA sees the full accumulated codebase, not just the latest delta.
        prev_context = [cf for cf in task["meta"].get("context_files", []) if cf != output_path]
        qa_context_files = [output_path] + prev_context

        # Idempotency guard: if a QA task already references this output (e.g. this
        # coder task crashed after creating QA but before advancing, and crash
        # recovery re-ran it), reuse it instead of creating a duplicate. output_path
        # carries this task's unique id, so this only ever matches THIS task's own QA.
        existing_status, existing_qa = _find_qa_for_output(output_path)
        if existing_qa is not None:
            qa_task_path = Path(existing_qa["path"])
            log.info(
                f"QA task already exists for {Path(output_path).name} "
                f"({qa_task_path.name}, status={existing_status}) — not creating a duplicate"
            )
        else:
            qa_task_path = create_task_file(
                inbox_path=PROJECT_ROOT / "agents" / "qa" / "inbox",
                task_type="qa",
                description=task["meta"].get("original_description") or task["body"],
                expected_output="QA verdict: PASS or FAIL with feedback",
                assigned_to="qa",
                created_by=AGENT_NAME,
                chain_to=None,
                retry_count=task["meta"].get("retry_count", 0),
                original_description=task["meta"].get("original_description") or task["body"],
                context_files=qa_context_files,
                validation_context=qa_validation_context,
                parent_task_id=task["meta"].get("parent_task_id"),
            )
            log.info(f"Chained to QA agent with {len(qa_context_files)} context file(s)")

        # Verify-before-advance: a coder subtask must never reach validation/ without
        # its QA task on disk, or the validation gate skips its parent forever. Atomic
        # writes make create_task_file all-or-nothing, so this should always hold; if
        # it somehow doesn't, leave the coder in processing/ so recover_processing_subtasks
        # re-runs it (the idempotency guard above keeps the re-run from duplicating QA).
        if not Path(qa_task_path).exists():
            log.error(
                f"QA task for {task_id} is not on disk after chaining — NOT advancing to "
                f"validation/; leaving in processing/ for recovery to re-run."
            )
            return

    mark_awaiting_validation(task_path)
    log.info(f"Task {task_id} complete -> {output_path} (awaiting validation)")


def main():
    log = AgentLogger(AGENT_NAME)
    client = OllamaClient()

    if not client.is_available():
        log.error(f"Ollama is not reachable at {client.base_url} — aborting")
        sys.exit(1)

    tasks = list_pending_tasks(INBOX)
    if not tasks:
        log.info("Inbox empty — nothing to do")
        return

    log.info(f"Found {len(tasks)} task(s)")
    for task_path in tasks:
        try:
            task = read_task(task_path)
            process_task(task, client, log)
        except Exception as e:
            log.error(f"Unhandled error on {task_path.name}: {e}")


if __name__ == "__main__":
    main()
