"""
agent_chat.py — Chat agent with tool calling via Ollama.
"""

import sys
import os
from pathlib import Path
from typing import Generator, List, Dict, Callable, Optional

# Add scripts to path — MUST be before importing shared modules
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "scripts"))

# Set OLLAMA_API_KEY from config BEFORE importing any shared modules
try:
    from shared.config import load_config
    _config = load_config()
    _api_key = _config.web_search_api_key()
    if _api_key:
        os.environ["OLLAMA_API_KEY"] = _api_key
except Exception:
    pass

from shared.ollama_client import OllamaClient, OllamaError
from shared.rag_tool import rag_query
from shared.web_search import web_search, web_fetch


def call_chat_with_tools(
    model: str,
    system_prompt: str,
    history: List[Dict],
    user_message: str,
    max_tool_turns: int = 8,
    options: Optional[dict] = None,
    think: Optional[bool] = None,
) -> str:
    """
    Run the tool-calling loop. Manages session history internally (tool messages are ephemeral).

    Args:
        model: Ollama model name (e.g. "qwen3.5:9b")
        system_prompt: System prompt for the LLM
        history: Prior user/assistant message pairs (no tool messages)
        user_message: The user's current message
        max_tool_turns: Max tool calls before forcing a text response
        options: Ollama sampling options dict (temperature, top_p, top_k, etc.)
        think: When True enables the model's internal reasoning mode; False
               disables it; None leaves the model default.

    Returns:
        The assistant's final text response

    Raises:
        OllamaError on connectivity failure
    """
    client = OllamaClient()
    tools = [rag_query, web_search, web_fetch]

    # Build messages: system prompt + history + new user message.
    # (system_prompt was previously accepted but silently dropped, so the
    # model never saw the pipeline snapshot, tool guidance, or CREATE_TASK
    # instructions — which is also why it rarely produced tool calls.)
    messages: List[Dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    for turn in range(max_tool_turns):
        # Call LLM with tools available
        try:
            response = client.chat_with_tools(
                model=model,
                messages=messages,
                tools=tools,
                options=options,
                think=think,
            )
        except OllamaError:
            raise

        # Check response type
        if response.get("type") == "text":
            text = response.get("content", "").strip()
            if text:
                return text
            # Empty text, try again
            continue

        if response.get("type") == "tool_call":
            # chat_with_tools returns keys "name", "arguments", "raw_message".
            # (Earlier this function read "tool_name"/"tool_input" — those keys
            # don't exist, so every call fell through to the unknown-tool branch.)
            raw_message = response.get("raw_message")
            tool_name = response.get("name", "unknown")
            tool_input = response.get("arguments", {}) or {}

            # Dispatch tool
            try:
                if tool_name == "rag_query":
                    tool_result = rag_query(tool_input.get("query", ""), tool_input.get("top_k", 5))
                elif tool_name == "web_search":
                    tool_result = web_search(tool_input.get("query", ""))
                elif tool_name == "web_fetch":
                    tool_result = web_fetch(tool_input.get("url", ""))
                else:
                    tool_result = f"Unknown tool: {tool_name}"
            except Exception as e:
                tool_result = f"Tool error: {str(e)}"

            # Append the assistant's tool call (use the library's native Message
            # object when available so the schema round-trips correctly) and
            # the tool result as a role:tool message — matches research/qa.
            if raw_message is not None:
                messages.append(raw_message)
            else:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": tool_name, "arguments": tool_input}}],
                })
            messages.append({
                "role": "tool",
                "content": tool_result,
                "tool_name": tool_name,
            })
            continue

        # Unrecognized response type
        continue

    # Max turns exceeded — force a text response by removing tools
    try:
        response = client.chat_with_tools(
            model=model,
            messages=messages,
            tools=[],
            options=options,
            think=think,
        )
        if response.get("type") == "text":
            return response.get("content", "").strip() or "No response generated."
    except OllamaError:
        pass

    return "Tool limit exceeded. Please try a simpler request."


def stream_chat_with_tools(
    model: str,
    system_prompt: str,
    history: List[Dict],
    user_message: str,
    max_tool_turns: int = 8,
    options: Optional[dict] = None,
    think: Optional[bool] = None,
) -> Generator:
    """
    Streaming version of call_chat_with_tools.

    Phase 1 runs the tool-calling loop non-streaming (tools execute fast and
    the LLM response is tiny) yielding a ``tool_call`` event per dispatch.
    Phase 2 streams the final LLM response, yielding ``thinking`` / ``token``
    chunks, then a single ``done`` event with the complete assembled text.

    Yields dicts — one of:
      {"type": "tool_call", "name": str,  "args": dict}    — tool dispatched
      {"type": "thinking",  "text": str}                    — reasoning chunk
      {"type": "token",     "text": str}                    — content chunk
      {"type": "done",      "full_content": str}            — stream finished
      {"type": "error",     "message": str}                 — failure
    """
    client = OllamaClient()
    tools = [rag_query, web_search, web_fetch]

    messages: List[Dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # ── Phase 1: non-streaming tool loop ──────────────────────────────────
    for _turn in range(max_tool_turns):
        try:
            response = client.chat_with_tools(
                model=model,
                messages=messages,
                tools=tools,
                options=options,
                think=think,
            )
        except OllamaError as e:
            yield {"type": "error", "message": str(e)}
            return

        if response.get("type") == "text":
            # LLM is done with tools — break to Phase 2 streaming call.
            # We intentionally discard this text response and re-call the
            # LLM via stream_response so the user sees tokens as they arrive.
            break

        if response.get("type") == "tool_call":
            raw_message = response.get("raw_message")
            tool_name = response.get("name", "unknown")
            tool_input = response.get("arguments", {}) or {}

            try:
                if tool_name == "rag_query":
                    tool_result = rag_query(tool_input.get("query", ""), tool_input.get("top_k", 5))
                elif tool_name == "web_search":
                    tool_result = web_search(tool_input.get("query", ""))
                elif tool_name == "web_fetch":
                    tool_result = web_fetch(tool_input.get("url", ""))
                else:
                    tool_result = f"Unknown tool: {tool_name}"
            except Exception as e:
                tool_result = f"Tool error: {str(e)}"

            yield {"type": "tool_call", "name": tool_name, "args": tool_input}

            if raw_message is not None:
                messages.append(raw_message)
            else:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": tool_name, "arguments": tool_input}}],
                })
            messages.append({
                "role": "tool",
                "content": tool_result,
                "tool_name": tool_name,
            })
            continue

        # Unrecognized response shape — skip
        continue

    # ── Phase 2: stream the final response ────────────────────────────────
    full_content = ""
    try:
        for chunk in client.stream_response(
            model=model,
            messages=messages,
            options=options,
            think=think,
        ):
            if chunk["thinking"]:
                yield {"type": "thinking", "text": chunk["thinking"]}
            if chunk["content"]:
                full_content += chunk["content"]
                yield {"type": "token", "text": chunk["content"]}
            if chunk["done"]:
                break
    except OllamaError as e:
        yield {"type": "error", "message": str(e)}
        return

    yield {"type": "done", "full_content": full_content}
