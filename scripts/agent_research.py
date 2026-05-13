"""
agent_research.py — Research/summarization worker agent (qwen3.5:9b).

CRON: */2 * * * * /usr/bin/python3 /path/to/scripts/agent_research.py

Responsibilities:
  1. Poll agents/research/inbox/ for pending .task.md files
  2. Run an agentic tool loop: call qwen3.5:9b with web_search and web_fetch
     available as native tools; execute any calls the model requests, loop
     until final answer
  3. Write the result (summary, research, Q&A) to task's output_path
  4. Move task to outbox/ on success, failed/ on error

Web search: Ollama cloud API (https://ollama.com/api/web_search).
API key loaded from config.json → web_search.ollama_api_key.
The model decides when to search or fetch; max MAX_TOOL_TURNS iterations per task.
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
    PROJECT_ROOT,
)
from shared.ollama_client import OllamaClient, OllamaError
from shared.web_search import web_search, web_fetch
from shared.logger import AgentLogger
from shared.token_logger import log_tokens
from shared.config import load_config

AGENT_NAME = "research"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "research" / "inbox"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "research" / "system_prompt.md"

# Safety cap: maximum tool calls per task to prevent runaway loops
MAX_TOOL_TURNS = 5

# Native tools — the ollama library introspects these functions' type
# annotations and docstrings to auto-generate the JSON schemas.
TOOLS = [web_search, web_fetch]


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def build_initial_messages(system_prompt: str, user_message: str) -> list[dict]:
    """Build the starting message list for the agentic loop."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def run_agentic_loop(
    task_id: str,
    messages: list[dict],
    client: OllamaClient,
    log: AgentLogger,
) -> str:
    """
    Run the tool-calling loop until the model produces a final text answer
    or MAX_TOOL_TURNS is exhausted.

    Returns the final text response string.
    Raises OllamaError on unrecoverable API failures.
    """
    for turn in range(MAX_TOOL_TURNS):
        result = client.chat_with_tools(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )

        if result["type"] == "text":
            log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
            log.info(f"[{task_id}] Final answer received after {turn} tool turn(s)")
            return result["content"]

        if result["type"] == "tool_call":
            tool_name = result["name"]
            arguments = result["arguments"]

            if tool_name == "web_search":
                query = arguments.get("query", "").strip()
                if not query:
                    log.warning(f"[{task_id}] web_search called with empty query — skipping")
                    tool_result = "ERROR: 'query' parameter was empty. Please provide a search query."
                else:
                    log.info(f"[{task_id}] web_search({turn + 1}/{MAX_TOOL_TURNS}): {query!r}")
                    tool_result = web_search(query)
                    log.info(f"[{task_id}] web_search returned {len(tool_result)} chars")

            elif tool_name == "web_fetch":
                url = arguments.get("url", "").strip()
                if not url:
                    log.warning(f"[{task_id}] web_fetch called with empty url — skipping")
                    tool_result = "ERROR: 'url' parameter was empty. Please provide a URL."
                else:
                    log.info(f"[{task_id}] web_fetch({turn + 1}/{MAX_TOOL_TURNS}): {url!r}")
                    tool_result = web_fetch(url)
                    log.info(f"[{task_id}] web_fetch returned {len(tool_result)} chars")

            else:
                log.warning(f"[{task_id}] Model called unknown tool '{tool_name}' — skipping")
                tool_result = f"ERROR: Tool '{tool_name}' is not available."

            if tool_result.startswith("ERROR:") and tool_name in ("web_search", "web_fetch"):
                log.error(f"[{task_id}] Tool error — aborting loop: {tool_result}")
                raise OllamaError(f"Web tool unavailable: {tool_result}")

            # Append the assistant's tool call and the tool result to history.
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

    # MAX_TOOL_TURNS reached — ask for a final answer without tools
    log.warning(f"[{task_id}] Reached {MAX_TOOL_TURNS} tool turns — requesting final answer")
    messages.append({
        "role": "user",
        "content": (
            "You have reached the maximum number of web searches allowed. "
            "Please now provide your final answer based on the information gathered."
        ),
    })
    # One last plain chat call (no tools) to force a text response
    result = client.chat_with_tools(model=MODEL, messages=messages, tools=[])
    if result["type"] == "text":
        log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        return result["content"]
    log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
    return "(No final answer produced after maximum search iterations.)"


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    task_id = task["meta"].get("id", "unknown")
    log.info(f"Processing task {task_id}")

    task_path = mark_processing(task["path"])

    system_prompt = load_system_prompt()
    user_message = task["body"]

    # Inject any context files into the user message
    context_files = task["meta"].get("context_files", [])
    if context_files:
        context_content = []
        for cf in context_files:
            cf_path = Path(cf)
            if cf_path.exists():
                context_content.append(f"### {cf_path.name}\n\n{cf_path.read_text(encoding='utf-8')}")
        if context_content:
            user_message = "\n\n---\n\n".join(context_content) + "\n\n---\n\n" + user_message

    messages = build_initial_messages(system_prompt, user_message)

    try:
        response = run_agentic_loop(task_id, messages, client, log)
        log.info(f"Research response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error for {task_id}: {e}")
        mark_failed(task_path)
        return

    output_path = task["meta"].get("output_path")
    if not output_path:
        output_path = str(PROJECT_ROOT / "outbox" / f"{task_id}_result.md")

    write_result(output_path, response, meta={"task_id": task_id, "agent": AGENT_NAME, "model": MODEL})
    mark_awaiting_validation(task_path)
    log.info(f"Task {task_id} complete → {output_path} (awaiting validation)")


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
