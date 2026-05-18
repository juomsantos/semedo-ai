"""
Tests for ``scripts/shared/rag_injection.py``.

The helper used to be byte-for-byte duplicated in ``agent_coder.py`` and
``agent_orchestrator.py``. M2 consolidated it; these tests lock in the
filter semantics (which prefixes get dropped, what gets prepended, what
characters are sent to the RAG API) so any future change has to be
deliberate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import shared.rag_injection as ri


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_prepends_knowledge_base_context_when_rag_returns_results():
    """Useful RAG output is prepended with the canonical heading + separator."""
    with patch.object(ri, "rag_query", return_value="## doc_a\n\nfoo bar baz"):
        result = ri.inject_rag_context("Build a thing.")

    assert result.startswith("## Knowledge Base Context\n")
    assert "doc_a" in result
    assert "foo bar baz" in result
    assert "\n---\n\nBuild a thing." in result


def test_truncates_query_to_default_500_chars():
    """The RAG API is queried with the first DEFAULT_QUERY_CHAR_LIMIT chars only."""
    long_body = "x" * 5000
    with patch.object(ri, "rag_query", return_value="## doc\nhit") as mock:
        ri.inject_rag_context(long_body)
    args, _ = mock.call_args
    assert len(args[0]) == ri.DEFAULT_QUERY_CHAR_LIMIT == 500


def test_truncates_query_to_custom_char_limit():
    long_body = "x" * 5000
    with patch.object(ri, "rag_query", return_value="## doc\nhit") as mock:
        ri.inject_rag_context(long_body, char_limit=120)
    args, _ = mock.call_args
    assert len(args[0]) == 120


def test_short_body_is_passed_through_to_rag_query_in_full():
    """Bodies shorter than the limit are not padded."""
    short = "hello"
    with patch.object(ri, "rag_query", return_value="## doc\nhit") as mock:
        ri.inject_rag_context(short)
    args, _ = mock.call_args
    assert args[0] == "hello"


# ---------------------------------------------------------------------------
# Non-useful RAG outputs are dropped silently
# ---------------------------------------------------------------------------


def test_returns_body_unchanged_when_rag_unavailable():
    """The "Knowledge base unavailable" string must not be injected — the model
    can't act on it and it would just confuse the prompt."""
    msg = "Knowledge base unavailable (RAG API not running — start with the scheduler or RUN_RAG_API.bat)"
    with patch.object(ri, "rag_query", return_value=msg):
        result = ri.inject_rag_context("Build a thing.")
    assert result == "Build a thing."


def test_returns_body_unchanged_when_rag_errored():
    with patch.object(ri, "rag_query", return_value="Knowledge base error: HTTP 500"):
        result = ri.inject_rag_context("Build a thing.")
    assert result == "Build a thing."


def test_returns_body_unchanged_when_rag_has_no_results():
    with patch.object(
        ri, "rag_query", return_value="No results found in knowledge base for: 'foo'"
    ):
        result = ri.inject_rag_context("Build a thing.")
    assert result == "Build a thing."


def test_returns_body_unchanged_when_rag_returns_empty_string():
    with patch.object(ri, "rag_query", return_value=""):
        result = ri.inject_rag_context("Build a thing.")
    assert result == "Build a thing."


def test_returns_body_unchanged_when_rag_returns_none():
    """Defensive — rag_query is typed to return str, but None must not crash."""
    with patch.object(ri, "rag_query", return_value=None):
        result = ri.inject_rag_context("Build a thing.")
    assert result == "Build a thing."


# ---------------------------------------------------------------------------
# Edge cases on the task body
# ---------------------------------------------------------------------------


def test_empty_body_returned_as_is_without_calling_rag():
    """An empty body shouldn't even call the RAG API — nothing to query for."""
    with patch.object(ri, "rag_query") as mock:
        result = ri.inject_rag_context("")
    assert result == ""
    mock.assert_not_called()


def test_none_body_returned_as_is_without_calling_rag():
    with patch.object(ri, "rag_query") as mock:
        result = ri.inject_rag_context(None)  # type: ignore[arg-type]
    assert result is None
    mock.assert_not_called()


# ---------------------------------------------------------------------------
# Filter prefix list is the source of truth
# ---------------------------------------------------------------------------


def test_non_useful_prefixes_constant_includes_expected_strings():
    """If rag_tool.py ever changes one of its sentinel messages, this test
    will remind the next developer to update _NON_USEFUL_PREFIXES too."""
    assert "Knowledge base unavailable" in ri._NON_USEFUL_PREFIXES
    assert "Knowledge base error" in ri._NON_USEFUL_PREFIXES
    assert "No results found" in ri._NON_USEFUL_PREFIXES


# ---------------------------------------------------------------------------
# Filter sentinel strings — these must match rag_tool.py error messages
# ---------------------------------------------------------------------------


def test_filters_all_sentinel_messages_from_rag_query():
    """When RAG returns a sentinel error message, inject_rag_context drops it."""
    with patch.object(ri, "rag_query", return_value="Knowledge base unavailable (...)"):
        result = ri.inject_rag_context("Some task body.")
    assert result == "Some task body."


def test_filters_rag_error_message():
    """HTTP errors and API failures are filtered."""
    with patch.object(ri, "rag_query", return_value="Knowledge base error: HTTP 500"):
        result = ri.inject_rag_context("Some task body.")
    assert result == "Some task body."
