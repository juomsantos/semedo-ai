"""
agent_research.py — Research/summarization worker agent (qwen3.5:9b).

CRON: */2 * * * * /usr/bin/python3 /path/to/scripts/agent_research.py

Responsibilities:
  1. Poll agents/research/inbox/ for pending .task.md files
  2. Run an agentic tool loop: call qwen3.5:9b with web_search available,
     execute any search calls the model requests, loop until final answer
  3. Write the result (summary, research, Q&A) to task's output_path
  4. Move task to outbox/ on success, failed/ on error

Web search: DuckDuckGo via duckduckgo-search (no API key required).
The model decides when to search; max MAX_TOOL_TURNS iterations per task.
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
from shared.web_search import web_search
from shared.logger import AgentLogger
from shared.config import load_config

AGENT_NAME = "research"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
INBOX = PROJECT_ROOT / "agents" / "research" / "inbox"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "research" / "system_prompt.md"

# Safety cap: maximum search calls per task to prevent runaway loops
MAX_TOOL_TURNS = 5

# Tool definition sent to the model on every request
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web using DuckDuckGo. Use this when you need current "
            "information, official documentation, recent events, or any fact "
            "that may be outside or beyond your training data."
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
    tools = [WEB_SEARCH_TOOL]

    for turn in range(MAX_TOOL_TURNS):
        result = client.chat_with_tools(
            model=MODEL,
            messages=messages,
            tools=tools,
        )

        if result["type"] == "text":
            log.info(f"[{task_id}] Final answer received after {turn} search turn(s)")
            return result["content"]

        if result["type"] == "tool_call":
            tool_name = result["name"]
            arguments = result["arguments"]

            if tool_name != "web_search":
                # Unknown tool — tell the model and continue
                log.warning(f"[{task_id}] Model called unknown tool '{tool_name}' — skipping")
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
                log.warning(f"[{task_id}] web_search called with empty query — skipping")
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "web_search", "arguments": arguments}}
                ]})
                messages.append({
                    "role": "tool",
                    "content": "ERROR: 'query' parameter was empty. Please provide a search query.",
                })
                continue

            log.info(f"[{task_id}] web_search({turn + 1}/{MAX_TOOL_TURNS}): {query!r}")
            search_results = web_search(query)
            log.info(f"[{task_id}] Search returned {len(search_results)} chars")

            if search_results.startswith("ERROR:"):
                log.error(f"[{task_id}] Search tool error — aborting loop: {search_results}")
                raise OllamaError(f"Web search unavailable: {search_results}")

            # Append the assistant's tool call and the tool result to history
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

    # MAX_TOOL_TURNS reached — ask for a final answer without tools
    log.warning(f"[{task_id}] Reached {MAX_TOOL_TURNS} search turns — requesting final answer")
    messages.append({
        "role": "user",
        "content": (
            "You have reached the maximum number of web searches allowed. "
            "Please now provide your final answer based on the information gathered."
        ),
    })
    # One last plain chat call (no tools) to force a text response
    fallback = client.chat(model=MODEL, system_prompt=None, user_message="", )
    # Re-issue as a full message list call via chat_with_tools with empty tools
    result = client.chat_with_tools(model=MODEL, messages=messages, tools=[])
    if result["type"] == "text":
        return result["content"]
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
                context_content.append(f"### {cf_path.name}\n\n{cf_path.read_text()}")
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
