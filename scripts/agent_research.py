"""
agent_research.py — Research/summarization worker agent (qwen3:9b).

CRON: */2 * * * * /usr/bin/python3 /path/to/scripts/agent_research.py

Responsibilities:
  1. Poll agents/research/inbox/ for pending .task.md files
  2. Call qwen3:9b with the task + system prompt
  3. Write the result (summary, research, Q&A) to task's output_path
  4. Move task to outbox/ on success, failed/ on error

Note: Same model as orchestrator (qwen3:9b) but different system prompt and role.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.task_io import (
    list_pending_tasks,
    read_task,
    write_result,
    mark_processing,
    mark_completed,
    mark_failed,
    PROJECT_ROOT,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.logger import AgentLogger

AGENT_NAME = "research"
MODEL = "qwen3.5:9b"
INBOX = PROJECT_ROOT / "agents" / "research" / "inbox"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "research" / "system_prompt.md"


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    task_id = task["meta"].get("id", "unknown")
    log.info(f"Processing task {task_id}")

    task_path = mark_processing(task["path"])

    system_prompt = load_system_prompt()
    user_message = task["body"]

    context_files = task["meta"].get("context_files", [])
    if context_files:
        context_content = []
        for cf in context_files:
            cf_path = Path(cf)
            if cf_path.exists():
                context_content.append(f"### {cf_path.name}\n\n{cf_path.read_text()}")
        if context_content:
            user_message = "\n\n---\n\n".join(context_content) + "\n\n---\n\n" + user_message

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message)
        log.info(f"Research response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error for {task_id}: {e}")
        mark_failed(task_path)
        return

    output_path = task["meta"].get("output_path")
    if not output_path:
        output_path = str(PROJECT_ROOT / "outbox" / f"{task_id}_result.md")

    write_result(output_path, response, meta={"task_id": task_id, "agent": AGENT_NAME, "model": MODEL})
    mark_completed(task_path)
    log.info(f"Task {task_id} complete → {output_path}")


def main():
    log = AgentLogger(AGENT_NAME)
    client = OllamaClient()

    if not client.is_available():
        log.error("Ollama is not reachable — aborting")
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
