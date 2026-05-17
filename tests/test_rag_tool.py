"""
Tests for ``scripts/shared/rag_tool.py``.

Coverage focus: the tool must always return a string, never raise — agents
include it in their Ollama tool loop and an exception would crash the loop.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
import requests

import shared.rag_tool as rag_tool


@pytest.fixture
def mock_post():
    with patch.object(rag_tool._requests, "post") as p:
        yield p


def _fake_response(status=200, payload=None, raise_for_status_error=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {}
    if raise_for_status_error:
        resp.raise_for_status.side_effect = raise_for_status_error
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_rag_query_formats_results_as_markdown(mock_post):
    mock_post.return_value = _fake_response(payload={
        "results": [
            {"score": 0.95, "document_id": "doc_a", "content": "Architecture overview..."},
            {"score": 0.80, "document_id": "doc_b", "content": "Validation loop..."},
        ]
    })
    out = rag_tool.rag_query("how does validation work?")
    assert "Knowledge base results" in out
    assert "doc_a" in out
    assert "doc_b" in out
    assert "0.950" in out
    assert "Architecture overview" in out


def test_rag_query_truncates_long_content(mock_post):
    long_text = "x" * 1000
    mock_post.return_value = _fake_response(payload={
        "results": [{"score": 0.9, "document_id": "doc", "content": long_text}]
    })
    out = rag_tool.rag_query("q")
    # Content is sliced to 400 chars per result
    # Output contains 400 x's plus header, so total length is bounded
    assert "x" * 400 in out
    assert "x" * 500 not in out


def test_rag_query_passes_top_k(mock_post):
    mock_post.return_value = _fake_response(payload={"results": []})
    rag_tool.rag_query("q", top_k=12)
    args, kwargs = mock_post.call_args
    assert kwargs["json"] == {"query": "q", "top_k": 12}


# ---------------------------------------------------------------------------
# Graceful degradation — these are the safety net for agent tool loops
# ---------------------------------------------------------------------------


def test_rag_query_returns_string_when_api_unreachable(mock_post):
    mock_post.side_effect = requests.exceptions.ConnectionError("refused")
    out = rag_tool.rag_query("q")
    assert isinstance(out, str)
    assert "Knowledge base unavailable" in out


def test_rag_query_returns_string_on_http_error(mock_post):
    mock_post.return_value = _fake_response(
        status=500,
        raise_for_status_error=requests.exceptions.HTTPError("500"),
    )
    out = rag_tool.rag_query("q")
    assert isinstance(out, str)
    assert "Knowledge base error" in out


def test_rag_query_returns_string_when_response_not_json(mock_post):
    resp = _fake_response()
    resp.json.side_effect = ValueError("not json")
    mock_post.return_value = resp
    out = rag_tool.rag_query("q")
    assert isinstance(out, str)
    assert "Knowledge base error" in out


def test_rag_query_returns_no_results_message_when_empty(mock_post):
    mock_post.return_value = _fake_response(payload={"results": []})
    out = rag_tool.rag_query("nothing here")
    assert "No results found" in out
    assert "nothing here" in out


def test_rag_query_returns_no_results_when_results_key_missing(mock_post):
    mock_post.return_value = _fake_response(payload={})
    out = rag_tool.rag_query("q")
    assert "No results found" in out


def test_rag_query_handles_timeout(mock_post):
    mock_post.side_effect = requests.exceptions.Timeout("slow")
    out = rag_tool.rag_query("q")
    assert isinstance(out, str)
    # Timeout falls into the broad except — different message but still a string
    assert "Knowledge base" in out
