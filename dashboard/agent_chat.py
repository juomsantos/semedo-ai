"""
agent_chat.py — Chat agent with tool calling via Ollama.
"""

import sys
import os
from pathlib import Path
from typing import List, Dict, Callable

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
) -> str:
    """
    Run the tool-calling loop. Manages session history internally (tool messages are ephemeral).

    Args:
        model: Ollama model name (e.g. "qwen3.5:9b")
        system_prompt: System prompt for the LLM
        history: Prior user/assistant message pairs (no tool messages)
        user_message: The user's current message
        max_tool_turns: Max tool calls before forcing a text response

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
        )
        if response.get("type") == "text":
            return response.get("content", "").strip() or "No response generated."
    except OllamaError:
        pass

    return "Tool limit exceeded. Please try a simpler request."
