"""
agent_claude_code.py — Claude Code CLI worker agent.

CRON: */3 * * * * /usr/bin/python3 /path/to/scripts/agent_claude_code.py

Responsibilities:
  1. Poll agents/claude-code/inbox/ for pending .task.md files
  2. Invoke `claude --print -p <task_content>` via subprocess
  3. Write the result to task's output_path
  4. Move task to outbox/ on success, failed/ on error

Prerequisites:
  - `claude` CLI must be installed and authenticated
  - Test with: claude --version

This is the escalation path for tasks requiring strong reasoning,
multi-step tool use, or anything the local models can't handle well.
"""

import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.task_io import (
    list_pending_tasks,
    read_task,
    write_result,
    mark_processing,
    mark_awaiting_validation,
    mark_failed,
    PROJECT_ROOT,
)
from shared.agent_boilerplate import build_user_message, log_tokens_safe
from shared.logger import AgentLogger
from shared.config import load_config

AGENT_NAME = "claude-code"
_config = load_config()
CLAUDE_TIMEOUT = _config.agent_timeout(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "claude-code" / "inbox"


# Preamble injected at the front of every prompt to prevent the CLI from
# attempting filesystem writes or requesting permissions in non-interactive mode.
_PIPELINE_PREAMBLE = (
    "You are processing a task submitted by an automated pipeline. "
    "Write your complete response as plain text in this reply — "
    "do NOT attempt to write files, use filesystem tools, or ask for "
    "permission to perform any action. Your full response will be "
    "automatically captured and saved by the pipeline.\n\n"
    "---\n\n"
)


def invoke_claude_code(task_body: str, log: AgentLogger) -> str:
    """
    Run `claude --print -p <prompt>` and return stdout.
    Raises subprocess.CalledProcessError on non-zero exit.
    """
    log.info("Invoking Claude Code CLI...")
    prompt = _PIPELINE_PREAMBLE + task_body
    result = subprocess.run(
        ["claude", "--print", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Claude Code exited with code {result.returncode}:\n{result.stderr}"
        )

    return result.stdout.strip()


def process_task(task: dict, log: AgentLogger):
    task_id = task["meta"].get("id", "unknown")
    log.info(f"Processing task {task_id}")

    task_path = mark_processing(task["path"])

    # Optionally prepend context files to the prompt
    user_message = build_user_message(task, style="claude-code", use_rag=False, logger=log)

    try:
        response = invoke_claude_code(user_message, log)
        log.info(f"Claude Code response received ({len(response)} chars)")
    except subprocess.TimeoutExpired:
        log.error(f"Claude Code timed out after {CLAUDE_TIMEOUT}s for task {task_id}")
        mark_failed(task_path)
        return
    except Exception as e:
        log.error(f"Claude Code invocation failed for {task_id}: {e}")
        mark_failed(task_path)
        return

    output_path = task["meta"].get("output_path")
    if not output_path:
        output_path = str(PROJECT_ROOT / "outbox" / f"{task_id}_result.md")

    write_result(output_path, response, meta={"task_id": task_id, "agent": AGENT_NAME})
    # Approximate completion-token count via word count — the Claude CLI does
    # not report tokens. M7 in REMAINING_ISSUES.md tracks replacing this with
    # real Anthropic SDK counts.
    log_tokens_safe(AGENT_NAME, task_id, response, fallback_completion=len(response.split()))
    mark_awaiting_validation(task_path)
    log.info(f"Task {task_id} complete → {output_path} (awaiting validation)")


def main():
    log = AgentLogger(AGENT_NAME)

    # Verify claude CLI is available
    try:
        subprocess.run(["claude", "--version"], capture_output=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.error(f"`claude` CLI not available: {e}")
        sys.exit(1)

    tasks = list_pending_tasks(INBOX)
    if not tasks:
        log.info("Inbox empty — nothing to do")
        return

    log.info(f"Found {len(tasks)} task(s)")
    for task_path in tasks:
        try:
            task = read_task(task_path)
            process_task(task, log)
        except Exception as e:
            log.error(f"Unhandled error on {task_path.name}: {e}")


if __name__ == "__main__":
    main()
