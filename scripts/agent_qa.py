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
    safe_read_context,
    PROJECT_ROOT,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.web_search import web_search, web_fetch
from shared.rag_tool import rag_query
from shared.agent_boilerplate import load_system_prompt, log_tokens_safe
from shared.logger import AgentLogger
from shared.config import load_config
from shared.validation_context import prepend_validation_context

AGENT_NAME = "qa"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
OPTIONS = _config.agent_options(AGENT_NAME)
THINKING = _config.agent_thinking(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "qa" / "inbox"

# Per-tool call limits for the QA agent
MAX_SEARCH_TURNS = 3   # max web_search calls per task
MAX_FETCH_TURNS  = 6   # max web_fetch calls per task
MAX_RAG_TURNS    = 5   # max rag_query calls per task
MAX_TOOL_TURNS = MAX_SEARCH_TURNS + MAX_FETCH_TURNS + MAX_RAG_TURNS

# Native tools — the ollama library introspects these functions' type
# annotations and docstrings to auto-generate the JSON schemas.
QA_TOOLS = [rag_query, web_search, web_fetch]

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


def extract_code(result_content: str) -> tuple:
    """
    Strip markdown fences from result file content.
    Returns (code, language) where language is 'python', 'javascript', or 'unknown'.
    For multi-file results, returns the first substantial code block found.
    """
    # Try JavaScript/TypeScript blocks first.
    # \b after the language name prevents ```js from matching ```json (js is a prefix of json).
    match = re.search(r"```(?:javascript|js|typescript|ts)\b\s*(.*?)\s*```", result_content, re.DOTALL)
    if match:
        return match.group(1), "javascript"

    # Try Python blocks
    match = re.search(r"```python\s*(.*?)\s*```", result_content, re.DOTALL)
    if match:
        return match.group(1), "python"

    # Fall back to generic ``` ... ``` (non-JSON blocks preferred)
    # Try to find a non-json, non-empty block
    for m in re.finditer(r"```(\w*)\s*(.*?)\s*```", result_content, re.DOTALL):
        lang_hint = m.group(1).lower()
        code = m.group(2)
        if lang_hint == "json" or not code.strip():
            continue
        if lang_hint in ("shell", "bash", "sh"):
            continue
        return code, "unknown"

    # Last resort: any code block
    match = re.search(r"```\w*\s*(.*?)\s*```", result_content, re.DOTALL)
    if match:
        return match.group(1), "unknown"

    # If no code blocks found, return the whole content as a fallback
    return result_content, "unknown"


_NOT_EXECUTED = object()  # sentinel — signals "no execution attempted"


def execute_code(code: str, language: str, log: AgentLogger):
    """
    Execute code for Python only. All other languages are skipped per QA policy:
    the LLM reviewer performs static analysis instead.

    Returns either:
      - A dict {stdout, stderr, exit_code, timed_out} for Python
      - The _NOT_EXECUTED sentinel for every other language
    """
    if language != "python":
        log.info(f"Language '{language}' — skipping execution (static analysis only per QA policy)")
        return _NOT_EXECUTED

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
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
    task_description: str, full_result: str, execution, prior_results: list,
    client: OllamaClient, log: AgentLogger, task_id: str = "unknown",
    validation_context: dict | None = None,
) -> dict:
    """
    Call qwen3.5:9b to review code with optional web search.
    full_result is the raw coder result file content — all files included.

    ``validation_context`` (M4): when the orchestrator issued a follow-up
    decision (redo/refine/additional_work) on the parent task and the coder
    chained the resulting QA subtask here, ``task_description`` came in via
    ``original_description`` and so does not carry the ``## Validation
    Context`` section that the QA system prompt explicitly expects. Passing
    the dict in lets us prepend the section to ``user_message`` so QA's
    review is calibrated to the decision type.

    Return {verdict: 'PASS'|'FAIL', feedback: str}.
    """
    system_prompt = load_system_prompt(AGENT_NAME)

    if execution is _NOT_EXECUTED:
        execution_section = "### Execution Output\n*Not executed — static analysis only (non-Python code).*\n"
    else:
        execution_section = "### Execution Output\n"
        if execution["timed_out"]:
            execution_section += "**Timed out** after 30 seconds\n"
        elif execution["exit_code"] == 0:
            execution_section += "**Exit code:** 0 (success)\n"
        else:
            execution_section += f"**Exit code:** {execution['exit_code']}\n"
        if execution["stdout"]:
            execution_section += f"\n**stdout:**\n```\n{execution['stdout']}\n```\n"
        if execution["stderr"]:
            execution_section += f"\n**stderr:**\n```\n{execution['stderr']}\n```\n"

    prior_context_section = ""
    if prior_results:
        prior_context_section = "\n## Prior Work (Previous Iterations — for context)\n\n"
        for i, prior in enumerate(prior_results, 1):
            prior_context_section += f"### Prior Result {i}\n\n{prior}\n\n"

    user_message = f"""## Original Task

{task_description}
{prior_context_section}
## Latest Code Produced

{full_result}

{execution_section}
Please review the latest code (and prior work context if provided) to determine if the submission correctly and completely solves the original task."""

    # M4: inject the orchestrator's validation context (if any) at the very
    # top so the QA system prompt's "look for ## Validation Context first"
    # instruction has something to find. No-op when the task wasn't a
    # follow-up.
    user_message = prepend_validation_context(user_message, validation_context)

    try:
        # Build initial messages for the agentic loop
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Run tool-calling loop up to MAX_TOOL_TURNS iterations
        response = None
        empty_response_retries = 0
        search_turns = 0
        fetch_turns  = 0
        rag_turns    = 0
        active_tools = list(QA_TOOLS)

        for turn in range(MAX_TOOL_TURNS):
            result = client.chat_with_tools(
                model=MODEL,
                messages=messages,
                tools=active_tools,
                options=OPTIONS,
                think=THINKING,
            )

            if result["type"] == "text":
                log_tokens_safe(AGENT_NAME, task_id, client)
                response = result["content"]
                log.info(f"QA review received ({len(response)} chars) after {turn} tool turn(s) (search={search_turns}/{MAX_SEARCH_TURNS}, fetch={fetch_turns}/{MAX_FETCH_TURNS}, rag={rag_turns}/{MAX_RAG_TURNS})")
                break

            if result["type"] == "tool_call":
                tool_name = result["name"]
                arguments = result["arguments"]

                if tool_name == "web_search":
                    search_turns += 1
                    query = arguments.get("query", "").strip()
                    if not query:
                        log.warning(f"web_search called with empty query — skipping")
                        tool_result = "ERROR: 'query' parameter was empty. Please provide a search query."
                    else:
                        log.info(f"web_search({search_turns}/{MAX_SEARCH_TURNS}): {query!r}")
                        tool_result = web_search(query)
                        log.info(f"web_search returned {len(tool_result)} chars")
                    if search_turns >= MAX_SEARCH_TURNS:
                        active_tools = [t for t in active_tools if t is not web_search]
                        log.info(f"web_search limit reached ({MAX_SEARCH_TURNS}) — removed from active tools")

                elif tool_name == "web_fetch":
                    fetch_turns += 1
                    url = arguments.get("url", "").strip()
                    if not url:
                        log.warning(f"web_fetch called with empty url — skipping")
                        tool_result = "ERROR: 'url' parameter was empty. Please provide a URL."
                    else:
                        log.info(f"web_fetch({fetch_turns}/{MAX_FETCH_TURNS}): {url!r}")
                        tool_result = web_fetch(url)
                        log.info(f"web_fetch returned {len(tool_result)} chars")
                    if fetch_turns >= MAX_FETCH_TURNS:
                        active_tools = [t for t in active_tools if t is not web_fetch]
                        log.info(f"web_fetch limit reached ({MAX_FETCH_TURNS}) — removed from active tools")

                elif tool_name == "rag_query":
                    rag_turns += 1
                    query = arguments.get("query", "").strip()
                    top_k = arguments.get("top_k", 5)
                    if not query:
                        log.warning(f"rag_query called with empty query — skipping")
                        tool_result = "ERROR: 'query' parameter was empty. Please provide a search query."
                    else:
                        log.info(f"rag_query({rag_turns}/{MAX_RAG_TURNS}): {query!r}")
                        tool_result = rag_query(query, top_k)
                        log.info(f"rag_query returned {len(tool_result)} chars")
                    if rag_turns >= MAX_RAG_TURNS:
                        active_tools = [t for t in active_tools if t is not rag_query]
                        log.info(f"rag_query limit reached ({MAX_RAG_TURNS}) — removed from active tools")

                else:
                    log.warning(f"Model called unknown tool '{tool_name}' — skipping")
                    tool_result = f"ERROR: Tool '{tool_name}' is not available."

                if tool_result.startswith("ERROR:") and tool_name in ("web_search", "web_fetch", "rag_query"):
                    # Log the failure but pass the error back to the LLM so it can
                    # decide how to proceed (retry, skip, or review without the lookup).
                    log.warning(f"{tool_name} error (passing to LLM): {tool_result}")

                # Append assistant tool call and tool result to history.
                # Use raw_message if available (preserves the library's native format).
                raw_msg = result.get("raw_message")
                if raw_msg is not None:
                    messages.append(raw_msg)
                else:
                    messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": tool_name, "arguments": arguments}}],
                    })
                messages.append({
                    "role": "tool",
                    "content": tool_result,
                    "tool_name": tool_name,
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
            result = client.chat_with_tools(model=MODEL, messages=messages, tools=[], options=OPTIONS, think=THINKING)
            if result["type"] == "text":
                log_tokens_safe(AGENT_NAME, task_id, client)
                response = result["content"]
                log.info(f"QA review received ({len(response)} chars) on final call")
            else:
                log_tokens_safe(AGENT_NAME, task_id, client)
                response = "(No final verdict produced after maximum search iterations.)"

        # Retry logic for empty responses
        while (response is None or response.strip() == "") and empty_response_retries < 2:
            empty_response_retries += 1
            log.warning(f"Received empty response from LLM, retrying ({empty_response_retries}/2)...")
            messages.append({
                "role": "user",
                "content": "Your previous response was empty. Please provide your QA review verdict now.",
            })
            result = client.chat_with_tools(model=MODEL, messages=messages, tools=[], options=OPTIONS, think=THINKING)
            if result["type"] == "text":
                log_tokens_safe(AGENT_NAME, task_id, client)
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


def handle_failure(task: dict, feedback: str, full_result: str, language: str, execution, log: AgentLogger):
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

        lang_label = {"python": "Python", "javascript": "JavaScript/Node.js"}.get(language, "code")
        # Pass the QA task's context_files (coder's previous output + prior iterations)
        # so the retry coder can see exactly what it wrote and make targeted fixes.
        retry_context_files = task["meta"].get("context_files", [])
        # Known gap (M4): the orchestrator's validation_context is intentionally
        # NOT forwarded onto this retry coder task. The retry prompt above
        # already embeds the QA FAIL feedback in the description, so re-injecting
        # the orchestrator's earlier redo/refine reasoning would mix two
        # perspectives ("the parent task wanted X" + "the last attempt failed
        # QA because Y") and risks confusing the coder. Reconsider if retry
        # coders start losing context about *why* a follow-up was created.
        new_task_path = create_task_file(
            inbox_path=coder_inbox,
            task_type="code",
            description=retry_prompt,
            expected_output=f"Fixed {lang_label} that passes QA review",
            assigned_to="coder",
            created_by=AGENT_NAME,
            chain_to="qa",
            retry_count=1,
            original_description=original_description,
            parent_task_id=task["meta"].get("parent_task_id"),
            context_files=retry_context_files,
        )
        log.info(f"Created retry task {new_task_path.name}")

        # Write a failure record to output_path so the dashboard and orchestrator
        # can find a result even for first-attempt failures (retry_count=0).
        task_output_path = task["meta"].get("output_path")
        if task_output_path:
            first_fail_report = f"""# QA Failure — Retry Dispatched

## Task ID
{task_id}

## QA Feedback
{feedback}

## Action Taken
A retry coder task has been dispatched ({new_task_path.name}).
"""
            write_result(
                task_output_path,
                first_fail_report,
                meta={"task_id": task_id, "agent": AGENT_NAME, "verdict": "FAIL"},
            )
            log.info(f"First-attempt failure record written to output_path: {task_output_path}")

    else:
        log.info(f"Task {task_id} failed QA on retry. Writing failure report...")

        if execution is _NOT_EXECUTED:
            execution_block = "*(Not executed — static analysis only)*"
        else:
            execution_block = (
                f"- **Exit Code:** {execution['exit_code']}\n"
                f"- **Timed Out:** {execution['timed_out']}\n\n"
                f"### stdout\n```\n{execution['stdout'] or '(none)'}\n```\n\n"
                f"### stderr\n```\n{execution['stderr'] or '(none)'}\n```"
            )

        failure_report = f"""# QA Failure Report

## Task ID
{task_id}

## Original Task Description
{task['meta'].get('original_description', task['body'])}

## Execution Output
{execution_block}

## QA Feedback
{feedback}

## Conclusion
Code failed QA review on the second attempt. Requires manual intervention or redesign.
"""

        # Write to failed/ for direct browsing
        archive_path = PROJECT_ROOT / "failed" / f"{task_id}_qa_failure.md"
        write_result(archive_path, failure_report)
        log.info(f"Failure report archived to {archive_path}")

        # ALSO write to output_path so the dashboard and orchestrator can find the
        # result via the task's output_path field (outbox/<id>_result.md).
        # Without this the dashboard shows "Result file not found" and the QA Results
        # tab has no record of the failure.
        task_output_path = task["meta"].get("output_path")
        if task_output_path:
            write_result(
                task_output_path,
                failure_report,
                meta={"task_id": task_id, "agent": AGENT_NAME, "verdict": "FAIL"},
            )
            log.info(f"Failure report written to output_path: {task_output_path}")


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

    # Get the context files (first = latest coder output; rest = prior iteration outputs)
    context_files = task["meta"].get("context_files", [])
    if not context_files:
        log.error(f"Task {task_id} has no context files (no coder result to review)")
        mark_failed(task_path)
        return

    coder_result_path = Path(context_files[0])
    result_content = safe_read_context(context_files[0], logger=log)
    if result_content is None:
        log.error(f"Primary context file unreadable or outside project root: {context_files[0]}")
        mark_failed(task_path)
        return

    if not result_content.strip():
        log.error(f"Empty result file: {coder_result_path}")
        mark_failed(task_path)
        return

    log.info(f"Loaded primary result: {len(result_content)} chars from {coder_result_path.name}")

    # Extract a single code block for Python execution only.
    # The full result_content (all files) is what gets sent to the LLM reviewer.
    code, language = extract_code(result_content)
    log.info(f"Detected language: {language} (used for execution gate only)")

    # Read any additional context files (prior coder iterations) for the LLM reviewer
    prior_results = []
    for cf in context_files[1:]:
        content = safe_read_context(cf, logger=log)
        if content is not None:
            prior_results.append(content)
            log.info(f"Loaded prior context: {Path(cf).name}")

    # Python execution only — extract_code gives us the runnable snippet
    execution = execute_code(code, language, log)
    if execution is _NOT_EXECUTED:
        log.info("Code execution skipped (static analysis only)")
    else:
        log.info(f"Code executed: exit_code={execution['exit_code']}, timed_out={execution['timed_out']}")

    task_description = task["meta"].get("original_description") or task["body"]
    # Pass the full result_content to the LLM — it sees every file the coder produced.
    # validation_context (M4) reaches QA via the coder→QA chain (agent_coder.py
    # forwards it on create_task_file) or directly when the orchestrator targets
    # QA in a follow-up. Either way it must be re-injected into user_message
    # because we feed `original_description` (intentionally VC-free) above.
    review = review_with_llm(
        task_description, result_content, execution, prior_results, client, log, task_id,
        validation_context=task["meta"].get("validation_context"),
    )
    log.info(f"QA verdict: {review['verdict']}")

    if review["verdict"] == "PASS":
        output_path = task["meta"].get("output_path")
        if not output_path:
            output_path = str(PROJECT_ROOT / "outbox" / f"{task_id}_qa_result.md")

        if execution is _NOT_EXECUTED:
            exec_summary = "*(Not executed — reviewed via static analysis)*"
        else:
            exec_summary = f"- **Exit Code:** {execution['exit_code']}\n- **Status:** Success"

        qa_result = f"""# QA Approval

Task {task_id} has passed QA review.

## Execution Result
{exec_summary}

This code is ready for production.
"""

        write_result(output_path, qa_result, meta={"task_id": task_id, "agent": AGENT_NAME, "verdict": "PASS"})
        mark_awaiting_validation(task_path)
        log.info(f"Task {task_id} passed QA → {output_path} (awaiting validation)")
    else:
        handle_failure(task, review["feedback"], result_content, language, execution, log)
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
