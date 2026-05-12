"""
agent_coder.py — Coder worker agent (qwen2.5-coder:7b).

CRON: */2 * * * * /usr/bin/python3 /path/to/scripts/agent_coder.py

Responsibilities:
  1. Poll agents/coder/inbox/ for pending .task.md files
  2. Call qwen2.5-coder:7b with the task + system prompt
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
from shared.logger import AgentLogger
from shared.token_logger import log_tokens
from shared.config import load_config

AGENT_NAME = "coder"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "coder" / "inbox"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "coder" / "system_prompt.md"


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    task_id = task["meta"].get("id", "unknown")

    # Skip tasks with unresolved dependencies
    if task["meta"].get("depends_on"):
        log.info(f"Skipping task {task_id} — unresolved dependencies: {task['meta']['depends_on']}")
        return

    log.info(f"Processing task {task_id}")

    task_path = mark_processing(task["path"])

    system_prompt = load_system_prompt()
    user_message = task["body"]

    # Include any context files referenced in the task
    context_files = task["meta"].get("context_files", [])
    if context_files:
        context_content = []
        for cf in context_files:
            cf_path = Path(cf)
            if cf_path.exists():
                context_content.append(f"### {cf_path.name}\n```\n{cf_path.read_text(encoding='utf-8')}\n```")
        if context_content:
            user_message = "\n\n".join(context_content) + "\n\n---\n\n" + user_message

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message)
        log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
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

        create_task_file(
            inbox_path=PROJECT_ROOT / "agents" / "qa" / "inbox",
            task_type="qa",
            description=task["meta"].get("original_description") or task["body"],
            expected_output="QA verdict: PASS or FAIL with feedback",
            assigned_to="qa",
            created_by=AGENT_NAME,
            chain_to=None,
            retry_count=task["meta"].get("retry_count", 0),
            original_description=task["meta"].get("original_description") or task["body"],
            context_files=[output_path],
            validation_context=qa_validation_context,
            parent_task_id=task["meta"].get("parent_task_id"),
        )
        log.info("Chained to QA agent")

    mark_awaiting_validation(task_path)
    log.info(f"Task {task_id} complete -> {output_path} (awaiting validation)")


def main():
    log = AgentLogger(AGENT_NAME)
    client = OllamaClient()

    if not client.is_available():
        log.error("Ollama is not reachable — aborting")
        import sys; sys.exit(1)

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
