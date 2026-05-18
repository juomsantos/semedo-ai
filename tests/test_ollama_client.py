"""
Tests for ``scripts/shared/ollama_client.py`` — the thin wrapper around the
Ollama Python library used by every agent.

We mock the underlying ``_ollama.Client`` so tests don't touch a network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import shared.ollama_client as oc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chat_response(content="hello", prompt_eval=10, eval_count=5, tool_calls=None):
    """Build a duck-typed ollama chat response."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        message=message,
        prompt_eval_count=prompt_eval,
        eval_count=eval_count,
    )


def _make_tool_call(name="web_search", arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments or {"q": "test"})
    return SimpleNamespace(function=function)


@pytest.fixture
def client():
    """OllamaClient with the inner _ollama.Client mocked."""
    c = oc.OllamaClient(base_url="http://test:11434", timeout=10)
    c._client = MagicMock()
    return c


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash():
    c = oc.OllamaClient(base_url="http://test:11434/")
    assert c.base_url == "http://test:11434"


def test_init_stores_timeout():
    c = oc.OllamaClient(timeout=42)
    assert c.timeout == 42


def test_init_starts_with_zero_token_counts():
    c = oc.OllamaClient()
    assert c.last_token_counts == {"prompt": 0, "completion": 0}


# ---------------------------------------------------------------------------
# chat() — happy path + error mapping
# ---------------------------------------------------------------------------


def test_chat_returns_message_content(client):
    client._client.chat.return_value = _make_chat_response(content="the answer")
    result = client.chat(model="qwen3:9b", user_message="hello")
    assert result == "the answer"


def test_chat_records_token_counts(client):
    client._client.chat.return_value = _make_chat_response(prompt_eval=99, eval_count=42)
    client.chat(model="qwen3:9b", user_message="hello")
    assert client.last_token_counts == {"prompt": 99, "completion": 42}


def test_chat_includes_system_prompt(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", system_prompt="sys")
    sent_messages = client._client.chat.call_args.kwargs["messages"]
    assert sent_messages[0] == {"role": "system", "content": "sys"}
    assert sent_messages[1] == {"role": "user", "content": "u"}


def test_chat_omits_system_when_none(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u")
    sent_messages = client._client.chat.call_args.kwargs["messages"]
    assert len(sent_messages) == 1
    assert sent_messages[0]["role"] == "user"


def test_chat_passes_temperature_in_options(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", temperature=0.9)
    assert client._client.chat.call_args.kwargs["options"] == {"temperature": 0.9}


def test_chat_merges_options_with_temperature(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", options={"top_p": 0.8, "top_k": 40})
    sent = client._client.chat.call_args.kwargs["options"]
    assert sent == {"temperature": 0.3, "top_p": 0.8, "top_k": 40}


def test_chat_options_override_temperature(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", temperature=0.3, options={"temperature": 0.7})
    assert client._client.chat.call_args.kwargs["options"]["temperature"] == 0.7


def test_chat_passes_min_p_passthrough(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", options={"min_p": 0.05, "seed": 42, "stop": ["</s>"]})
    sent = client._client.chat.call_args.kwargs["options"]
    assert sent["min_p"] == 0.05
    assert sent["seed"] == 42
    assert sent["stop"] == ["</s>"]


def test_chat_passes_think_when_set(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", think=True)
    assert client._client.chat.call_args.kwargs["think"] is True


def test_chat_omits_think_when_none(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u")
    assert "think" not in client._client.chat.call_args.kwargs


def test_chat_options_none_uses_only_default_temperature(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat(model="m", user_message="u", options=None)
    assert client._client.chat.call_args.kwargs["options"] == {"temperature": 0.3}


def test_chat_raises_OllamaError_on_response_error(client):
    client._client.chat.side_effect = oc._ollama.ResponseError("server boom")
    with pytest.raises(oc.OllamaError, match="API error"):
        client.chat(model="m", user_message="u")


def test_chat_maps_connection_error_to_friendly_message(client):
    client._client.chat.side_effect = RuntimeError("connection refused")
    with pytest.raises(oc.OllamaError, match="Cannot connect"):
        client.chat(model="m", user_message="u")


def test_chat_maps_timeout_to_friendly_message(client):
    client._client.chat.side_effect = RuntimeError("request timed out")
    with pytest.raises(oc.OllamaError, match="timed out"):
        client.chat(model="m", user_message="u")


def test_chat_maps_other_errors_to_generic_OllamaError(client):
    client._client.chat.side_effect = RuntimeError("something unexpected")
    with pytest.raises(oc.OllamaError, match="Ollama error"):
        client.chat(model="m", user_message="u")


def test_chat_returns_empty_string_when_content_none(client):
    client._client.chat.return_value = _make_chat_response(content=None)
    assert client.chat(model="m", user_message="u") == ""


# ---------------------------------------------------------------------------
# chat_with_tools()
# ---------------------------------------------------------------------------


def test_chat_with_tools_returns_text_response(client):
    client._client.chat.return_value = _make_chat_response(content="final answer")
    result = client.chat_with_tools(model="m", messages=[{"role": "user", "content": "u"}], tools=[])
    assert result == {"type": "text", "content": "final answer"}


def test_chat_with_tools_returns_tool_call_dict(client):
    tool_call = _make_tool_call(name="web_search", arguments={"q": "x"})
    client._client.chat.return_value = _make_chat_response(content=None, tool_calls=[tool_call])
    result = client.chat_with_tools(model="m", messages=[], tools=[lambda q: q])
    assert result["type"] == "tool_call"
    assert result["name"] == "web_search"
    assert result["arguments"] == {"q": "x"}
    assert "raw_message" in result


def test_chat_with_tools_parses_string_json_arguments(client):
    tool_call = _make_tool_call(arguments='{"q": "json-encoded"}')
    client._client.chat.return_value = _make_chat_response(content=None, tool_calls=[tool_call])
    result = client.chat_with_tools(model="m", messages=[], tools=[lambda q: q])
    assert result["arguments"] == {"q": "json-encoded"}


def test_chat_with_tools_returns_raw_arguments_when_json_invalid(client):
    tool_call = _make_tool_call(arguments="not json {")
    client._client.chat.return_value = _make_chat_response(content=None, tool_calls=[tool_call])
    result = client.chat_with_tools(model="m", messages=[], tools=[lambda q: q])
    assert result["arguments"] == {"raw": "not json {"}


def test_chat_with_tools_passes_none_when_tools_empty(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat_with_tools(model="m", messages=[], tools=[])
    assert client._client.chat.call_args.kwargs["tools"] is None


def test_chat_with_tools_merges_options_with_temperature(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat_with_tools(model="m", messages=[], tools=[], options={"top_p": 0.8, "min_p": 0.05})
    sent = client._client.chat.call_args.kwargs["options"]
    assert sent == {"temperature": 0.3, "top_p": 0.8, "min_p": 0.05}


def test_chat_with_tools_options_override_temperature(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat_with_tools(model="m", messages=[], tools=[], options={"temperature": 0.9})
    assert client._client.chat.call_args.kwargs["options"]["temperature"] == 0.9


def test_chat_with_tools_passes_think_when_set(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat_with_tools(model="m", messages=[], tools=[], think=False)
    assert client._client.chat.call_args.kwargs["think"] is False


def test_chat_with_tools_omits_think_when_none(client):
    client._client.chat.return_value = _make_chat_response()
    client.chat_with_tools(model="m", messages=[], tools=[])
    assert "think" not in client._client.chat.call_args.kwargs


def test_chat_with_tools_raises_OllamaError_on_connection_error(client):
    client._client.chat.side_effect = RuntimeError("connection refused")
    with pytest.raises(oc.OllamaError, match="Cannot connect"):
        client.chat_with_tools(model="m", messages=[], tools=[])


def test_chat_with_tools_raises_OllamaError_on_timeout(client):
    client._client.chat.side_effect = RuntimeError("timed out")
    with pytest.raises(oc.OllamaError, match="timed out"):
        client.chat_with_tools(model="m", messages=[], tools=[])


def test_chat_with_tools_raises_OllamaError_on_response_error(client):
    client._client.chat.side_effect = oc._ollama.ResponseError("server boom")
    with pytest.raises(oc.OllamaError, match="API error"):
        client.chat_with_tools(model="m", messages=[], tools=[])


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


def test_is_available_true_when_list_succeeds(client):
    client._client.list.return_value = SimpleNamespace(models=[])
    assert client.is_available() is True


def test_is_available_false_on_any_exception(client):
    client._client.list.side_effect = RuntimeError("boom")
    assert client.is_available() is False


# ---------------------------------------------------------------------------
# list_models()
# ---------------------------------------------------------------------------


def test_list_models_returns_model_names(client):
    client._client.list.return_value = SimpleNamespace(
        models=[SimpleNamespace(model="qwen3:9b"), SimpleNamespace(model="llama3:8b")]
    )
    assert client.list_models() == ["qwen3:9b", "llama3:8b"]


def test_list_models_raises_OllamaError_on_failure(client):
    client._client.list.side_effect = RuntimeError("boom")
    with pytest.raises(oc.OllamaError, match="Could not list models"):
        client.list_models()
