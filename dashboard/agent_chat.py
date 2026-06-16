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
from ollama_api_logger import OllamaAPILogger

# Initialize logger
dashboard_dir = Path(__file__).resolve().parent
logs_dir = project_root / "logs" / "dashboard"
_api_logger = OllamaAPILogger(logs_dir)


def call_chat_with_tools(
    model: str,
    system_prompt: str,
    history: List[Dict],
    user_message: str,
    max_tool_turns: int = 8,
    options: Optional[dict] = None,
    think: Optional[bool] = None,
    session_id: Optional[str] = None,
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

    # The caller passes the real chat session id; the dashboard's Ollama-API
    # log view filters on it. (The legacy fallback of parsing it from a
    # "[session_id] message" prefix is gone — the message is actually prefixed
    # with a *timestamp*, so that parse keyed every log entry by timestamp and
    # made true per-session filtering impossible.)

    for turn in range(max_tool_turns):
        # Call LLM with tools available
        try:
            _api_logger.log_request(model, messages, tools, options, session_id)
            response = client.chat_with_tools(
                model=model,
                messages=messages,
                tools=tools,
                options=options,
                think=think,
            )
            _api_logger.log_response(response, session_id)
        except OllamaError as e:
            _api_logger.log_error(str(e), {"turn": turn}, session_id)
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
        _api_logger.log_request(model, messages, [], options, session_id)
        response = client.chat_with_tools(
            model=model,
            messages=messages,
            tools=[],
            options=options,
            think=think,
        )
        _api_logger.log_response(response, session_id)
        if response.get("type") == "text":
            return response.get("content", "").strip() or "No response generated."
    except OllamaError as e:
        _api_logger.log_error(str(e), {"context": "max_turns_exceeded"}, session_id)
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
    session_id: Optional[str] = None,
) -> Generator:
    """
    Single-pass streaming chat with tool calling.

    Drives the whole conversation through ``OllamaClient.stream_with_tools`` —
    one streaming LLM call per turn. A turn streams ``thinking`` / ``token``
    events live; if the model emits tool calls, they are dispatched and the loop
    streams the next turn. The first turn that produces no tool calls *is* the
    final answer (no separate non-streaming probe call, no discarded generation).

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

    # session_id is passed explicitly by the caller (the dashboard's real chat
    # session id, which the Ollama-API log view filters on). Don't derive it
    # from the message — that text is prefixed with a timestamp, not the id.

    full_content = ""

    for turn in range(max_tool_turns):
        # On every turn but a forced final one, tools stay enabled.
        turn_content = ""
        turn_tool_calls: List[Dict] = []

        try:
            _api_logger.log_request(model, messages, tools, options, session_id)
            for chunk in client.stream_with_tools(
                model=model,
                messages=messages,
                tools=tools,
                options=options,
                think=think,
            ):
                _api_logger.log_stream_chunk(chunk, session_id)
                if chunk["thinking"]:
                    yield {"type": "thinking", "text": chunk["thinking"]}
                if chunk["content"]:
                    turn_content += chunk["content"]
                    yield {"type": "token", "text": chunk["content"]}
                if chunk["tool_calls"]:
                    turn_tool_calls.extend(chunk["tool_calls"])
                if chunk["done"]:
                    break
        except OllamaError as e:
            _api_logger.log_error(str(e), {"turn": turn}, session_id)
            yield {"type": "error", "message": str(e)}
            return

        # No tool calls → this streamed text is the final answer.
        if not turn_tool_calls:
            full_content += turn_content
            yield {"type": "done", "full_content": full_content}
            return

        # Otherwise dispatch the tools and loop for the next turn. The content
        # streamed during a tool turn (usually empty) is intermediate narration,
        # so it is not folded into the final answer.
        messages.append({
            "role": "assistant",
            "content": turn_content,
            "tool_calls": [
                {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in turn_tool_calls
            ],
        })

        for tc in turn_tool_calls:
            tool_name = tc.get("name", "unknown")
            tool_input = tc.get("arguments", {}) or {}

            yield {"type": "tool_call", "name": tool_name, "args": tool_input}

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

            messages.append({
                "role": "tool",
                "content": tool_result,
                "tool_name": tool_name,
            })

    # Tool-turn limit reached — force a final answer with tools disabled.
    try:
        _api_logger.log_request(model, messages, [], options, session_id)
        for chunk in client.stream_with_tools(
            model=model,
            messages=messages,
            tools=[],
            options=options,
            think=think,
        ):
            _api_logger.log_stream_chunk(chunk, session_id)
            if chunk["thinking"]:
                yield {"type": "thinking", "text": chunk["thinking"]}
            if chunk["content"]:
                full_content += chunk["content"]
                yield {"type": "token", "text": chunk["content"]}
            if chunk["done"]:
                break
    except OllamaError as e:
        _api_logger.log_error(str(e), {"context": "max_turns_exceeded"}, session_id)
        yield {"type": "error", "message": str(e)}
        return

    yield {"type": "done", "full_content": full_content}
