"""
agent_qa.py — QA worker agent (qwen3.5:9b).

CRON: */2 * * * * /usr/bin/python3 /path/to/scripts/agent_qa.py

Responsibilities:
  1. Poll agents/qa/inbox/ for pending .task.md files
  2. Extract Python code from the referenced result file
  3. Execute the code via subprocess with 30s timeout
  4. Call qwen3.5:9b to review: original task + code + execution output
  5. Return verdict: PASS (move to outbox) or FAIL (retry or report)
  6. If FAIL and retry_count==0: create new coder task with QA feedback
  7. If FAIL and retry_count==1: write detailed failure report to failed/
"""

import sys
import os
import atexit
import re
import subprocess
import tempfile
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
from shared.web_search import web_search
from shared.token_logger import log_tokens
from shared.logger import AgentLogger
from shared.config import load_config

AGENT_NAME = "qa"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "qa" / "inbox"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "qa" / "system_prompt.md"

# Safety cap: maximum search calls per task
MAX_TOOL_TURNS = 3

LOCK_FILE = PROJECT_ROOT / "agents" / "qa" / "qa.lock"


def _pid_exists(pid: int) -> bool:
    """Cross-platform check: is this PID still alive?"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def acquire_lock(log) -> bool:
    """
    Try to write a lockfile containing our PID.
    Returns True if acquired, False if another instance is already running.
    Stale locks (dead PID) are removed automatically.
    """
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_exists(pid):
                log.info(f"QA agent already running (PID {pid}) — skipping this run")
                return False
            else:
                log.warning(f"Removing stale lockfile (PID {pid} no longer running)")
                LOCK_FILE.unlink()
        except Exception:
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile. Registered with atexit so it runs on clean exit or exception."""
    LOCK_FILE.unlink(missing_ok=True)

# Tool definition sent to the model on every request
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web using DuckDuckGo. Use this to look up runtime errors, "
            "library documentation, or code patterns when execution output alone is insufficient."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A concise, specific search query.",
                }
            },
            "required": ["query"],
        },
    },
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def extract_code(result_content: str) -> str:
    """
    Strip markdown fences from result file content to get raw Python.
    Looks for ```python ... ``` or ``` ... ``` code blocks.
    """
    # Try to extract code from ```python ... ``` first
    match = re.search(r"```python\s*(.*?)\s*```", result_content, re.DOTALL)
    if match:
        return match.group(1)

    # Fall back to generic ``` ... ```
    match = re.search(r"```\s*(.*?)\s*```", result_content, re.DOTALL)
    if match:
        return match.group(1)

    # If no code blocks found, return the whole content as a fallback
    return result_content


def execute_code(code: str, log: AgentLogger) -> dict:
    """
    Run code via subprocess. Return {stdout, stderr, exit_code, timed_out}.
    Uses a temp file to avoid eval/exec security issues.
    30 second timeout enforced.
    """
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, temp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "Code execution timed out after 30 seconds",
                "exit_code": -1,
                "timed_out": True,
            }
        finally:
            Path(temp_path).unlink(missing_ok=True)
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Error executing code: {str(e)}",
            "exit_code": -1,
            "timed_out": False,
        }


def review_with_llm(
    task_description: str, code: str, execution: dict, client: OllamaClient, log: AgentLogger, task_id: str = "unknown"
) -> dict:
    """
    Call qwen3.5:9b to review code with optional web search.
    Return {verdict: 'PASS'|'FAIL', feedback: str}.
    """
    system_prompt = load_system_prompt()

    execution_report = f"### Execution Output\n"
    if execution["timed_out"]:
        execution_report += "**Timed out** after 30 seconds\n"
    elif execution["exit_code"] == 0:
        execution_report += f"**Exit code:** 0 (success)\n"
    else:
        execution_report += f"**Exit code:** {execution['exit_code']}\n"

    if execution["stdout"]:
        execution_report += f"\n**stdout:**\n```\n{execution['stdout']}\n```\n"
    if execution["stderr"]:
        execution_report += f"\n**stderr:**\n```\n{execution['stderr']}\n```\n"

    user_message = f"""## Original Task

{task_description}

## Code Produced

```python
{code}
```

{execution_report}

Please review this code and determine if it correctly solves the task."""

    try:
        # Build initial messages for the agentic loop
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Run tool-calling loop up to MAX_TOOL_TURNS iterations
        tools = [WEB_SEARCH_TOOL]
        response = None
        empty_response_retries = 0

        for turn in range(MAX_TOOL_TURNS):
            result = client.chat_with_tools(
                model=MODEL,
                messages=messages,
                tools=tools,
            )

            if result["type"] == "text":
                log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
                response = result["content"]
                log.info(f"QA review received ({len(response)} chars) after {turn} search turn(s)")
                break

            if result["type"] == "tool_call":
                tool_name = result["name"]
                arguments = result["arguments"]

                if tool_name != "web_search":
                    # Unknown tool — tell the model and continue
                    log.warning(f"Model called unknown tool '{tool_name}' — skipping")
                    messages.append({"role": "assistant", "content": "", "tool_calls": [
                        {"function": {"name": tool_name, "arguments": arguments}}
                    ]})
                    messages.append({
                        "role": "tool",
                        "content": f"ERROR: Tool '{tool_name}' is not available.",
                    })
                    continue

                query = arguments.get("query", "").strip()
                if not query:
                    log.warning(f"web_search called with empty query — skipping")
                    messages.append({"role": "assistant", "content": "", "tool_calls": [
                        {"function": {"name": "web_search", "arguments": arguments}}
                    ]})
                    messages.append({
                        "role": "tool",
                        "content": "ERROR: 'query' parameter was empty. Please provide a search query.",
                    })
                    continue

                log.info(f"web_search({turn + 1}/{MAX_TOOL_TURNS}): {query!r}")
                search_results = web_search(query)
                log.info(f"Search returned {len(search_results)} chars")

                if search_results.startswith("ERROR:"):
                    log.error(f"Search tool error — aborting loop: {search_results}")
                    raise OllamaError(f"Web search unavailable: {search_results}")

                # Append assistant tool call and tool result to history
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "web_search", "arguments": arguments}}
                    ],
                })
                messages.append({
                    "role": "tool",
                    "content": search_results,
                })

        # If MAX_TOOL_TURNS reached without a text response, request final answer
        if response is None:
            log.warning(f"Reached {MAX_TOOL_TURNS} search turns — requesting final answer")
            messages.append({
                "role": "user",
                "content": (
                    "You have reached the maximum number of web searches allowed. "
                    "Please now provide your final review verdict based on the information gathered."
                ),
            })
            result = client.chat_with_tools(model=MODEL, messages=messages, tools=[])
            if result["type"] == "text":
                log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
                response = result["content"]
                log.info(f"QA review received ({len(response)} chars) on final call")
            else:
                log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
                response = "(No final verdict produced after maximum search iterations.)"

        # Retry logic for empty responses
        while (response is None or response.strip() == "") and empty_response_retries < 2:
            empty_response_retries += 1
            log.warning(f"Received empty response from LLM, retrying ({empty_response_retries}/2)...")
            messages.append({
                "role": "user",
                "content": "Your previous response was empty. Please provide your QA review verdict now.",
            })
            result = client.chat_with_tools(model=MODEL, messages=messages, tools=[])
            if result["type"] == "text":
                log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
                response = result["content"]
                log.info(f"QA review received ({len(response)} chars) on retry {empty_response_retries}")

        # Parse verdict and feedback from response
        if response and response.strip() == "":
            log.error(f"QA received empty response after {empty_response_retries} retry attempts — defaulting to FAIL")
            return {"verdict": "FAIL", "feedback": "QA review produced no response after multiple retry attempts"}
        elif "VERDICT: PASS" in response.upper():
            return {"verdict": "PASS", "feedback": ""}
        elif "VERDICT: FAIL" in response.upper():
            feedback_match = re.search(r"FEEDBACK:\s*(.*)", response, re.DOTALL)
            feedback = feedback_match.group(1).strip() if feedback_match else "Review failed but no specific feedback provided"
            return {"verdict": "FAIL", "feedback": feedback}
        else:
            log.warning(f"Could not parse verdict from response: {response[:200]}")
            return {"verdict": "FAIL", "feedback": f"QA review was unclear: {response[:200]}"}

    except OllamaError as e:
        log.error(f"Ollama error during QA review: {e}")
        return {"verdict": "FAIL", "feedback": f"QA review failed: {str(e)}"}


def handle_failure(task: dict, feedback: str, code: str, execution: dict, log: AgentLogger):
    """
    Handle a failed QA review.
    If retry_count==0: create new coder task with QA feedback
    If retry_count==1: write detailed failure report to failed/
    """
    retry_count = task["meta"].get("retry_count", 0)
    task_id = task["meta"].get("id", "unknown")

    if retry_count == 0:
        log.info(f"Task {task_id} failed QA. Creating retry coder task...")

        original_description = task["meta"].get("original_description", task["body"])
        coder_inbox = PROJECT_ROOT / "agents" / "coder" / "inbox"

        retry_prompt = f"""## Original Task

{original_description}

## Previous Attempt Failed

The QA agent found the following issues:

{feedback}

Please fix these issues and try again."""

        new_task_path = create_task_file(
            inbox_path=coder_inbox,
            task_type="code",
            description=retry_prompt,
            expected_output="Fixed Python code that passes QA review",
            assigned_to="coder",
            created_by=AGENT_NAME,
            chain_to="qa",
            retry_count=1,
            original_description=original_description,
            parent_task_id=task["meta"].get("parent_task_id"),
        )
        log.info(f"Created retry task {new_task_path.name}")

    else:
        log.info(f"Task {task_id} failed QA on retry. Writing failure report...")

        failure_report = f"""# QA Failure Report

## Task ID
{task_id}

## Original Task Description
{task['meta'].get('original_description', task['body'])}

## Code Produced
```python
{code}
```

## Execution Output
- **Exit Code:** {execution['exit_code']}
- **Timed Out:** {execution['timed_out']}

### stdout
```
{execution['stdout'] or '(none)'}
```

### stderr
```
{execution['stderr'] or '(none)'}
```

## QA Feedback
{feedback}

## Conclusion
Code failed QA review on the second attempt. Requires manual intervention or redesign.
"""

        output_path = PROJECT_ROOT / "failed" / f"{task_id}_qa_failure.md"
        write_result(output_path, failure_report)
        log.info(f"Failure report written to {output_path}")


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    """
    Process a QA task.
    1. Extract code from the referenced result file
    2. Execute the code
    3. Review with LLM
    4. Handle pass/fail accordingly
    """
    task_id = task["meta"].get("id", "unknown")
    log.info(f"Processing task {task_id}")

    task_path = mark_processing(task["path"])

    # Get the context file (the coder's result file)
    context_files = task["meta"].get("context_files", [])
    if not context_files:
        log.error(f"Task {task_id} has no context files (no coder result to review)")
        mark_failed(task_path)
        return

    coder_result_path = Path(context_files[0])
    if not coder_result_path.exists():
        log.error(f"Context file not found: {coder_result_path}")
        mark_failed(task_path)
        return

    result_content = coder_result_path.read_text(encoding="utf-8")
    code = extract_code(result_content)

    if not code.strip():
        log.error(f"No code found in {coder_result_path}")
        mark_failed(task_path)
        return

    log.info(f"Extracted {len(code)} chars of code")

    execution = execute_code(code, log)
    log.info(f"Code executed: exit_code={execution['exit_code']}, timed_out={execution['timed_out']}")

    task_description = task["meta"].get("original_description") or task["body"]
    review = review_with_llm(task_description, code, execution, client, log, task_id)
    log.info(f"QA verdict: {review['verdict']}")

    if review["verdict"] == "PASS":
        output_path = task["meta"].get("output_path")
        if not output_path:
            output_path = str(PROJECT_ROOT / "outbox" / f"{task_id}_qa_result.md")

        qa_result = f"""# QA Approval

Task {task_id} has passed QA review.

## Code
```python
{code}
```

## Execution Result
- **Exit Code:** {execution['exit_code']}
- **Status:** Success

This code is ready for production.
"""

        write_result(output_path, qa_result, meta={"task_id": task_id, "agent": AGENT_NAME, "verdict": "PASS"})
        mark_awaiting_validation(task_path)
        log.info(f"Task {task_id} passed QA → {output_path} (awaiting validation)")
    else:
        handle_failure(task, review["feedback"], code, execution, log)
        mark_awaiting_validation(task_path)
        log.info(f"Task {task_id} failed QA → handled (awaiting validation)")


def main():
    log = AgentLogger(AGENT_NAME)

    if not acquire_lock(log):
        return

    atexit.register(release_lock)

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
