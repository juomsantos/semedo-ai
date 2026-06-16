"""
Tests for ``scripts/shared/agent_boilerplate.py``.

The four worker agents used to each re-implement load-system-prompt,
load-context-files, and token-logging. The helper consolidates those patterns;
these tests lock in the per-style rendering format (byte-for-byte) so any
future change has to be deliberate.

Parity matrix:

    style          fenced  header              join
    -----          ------  -----------------   -------------------------
    coder          yes     "### {name}"        "\\n\\n"
    research       no      "### {name}"        "\\n\\n---\\n\\n"
    claude-code    no      "### Context: {n}"  "\\n\\n"
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import shared.agent_boilerplate as ab
import shared.rag_injection as ri


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(body: str, context_files=None) -> dict:
    return {
        "body": body,
        "meta": {"context_files": list(context_files or [])},
        "path": Path("/tmp/fake.task.md"),
    }


# ---------------------------------------------------------------------------
# load_system_prompt
# ---------------------------------------------------------------------------


def test_load_system_prompt_reads_agent_file(fake_project):
    agent_dir = fake_project / "agents" / "foo"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "system_prompt.md").write_text("hello prompt\n", encoding="utf-8")

    assert ab.load_system_prompt("foo") == "hello prompt\n"


def test_load_system_prompt_handles_utf8(fake_project):
    """Non-ASCII content must round-trip (Windows default codec issue, C-class)."""
    agent_dir = fake_project / "agents" / "bar"
    agent_dir.mkdir(parents=True, exist_ok=True)
    content = "naïve café — café\n"
    (agent_dir / "system_prompt.md").write_text(content, encoding="utf-8")

    assert ab.load_system_prompt("bar") == content


def test_load_system_prompt_raises_for_missing_agent(fake_project):
    """No silent fallback — startup pre-flight relies on this raising."""
    with pytest.raises(FileNotFoundError):
        ab.load_system_prompt("does-not-exist")


# ---------------------------------------------------------------------------
# build_user_message — no context files, no RAG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["coder", "research", "claude-code"])
def test_build_user_message_returns_body_unchanged_when_no_context_and_no_rag(style):
    task = _make_task("Body of the task.")
    result = ab.build_user_message(task, style=style, use_rag=False)
    assert result == "Body of the task."


@pytest.mark.parametrize("style", ["coder", "research", "claude-code"])
def test_build_user_message_handles_missing_context_files_key(style):
    """Tasks without a ``context_files`` key must not crash."""
    task = {"body": "Body.", "meta": {}, "path": Path("/tmp/x.task.md")}
    assert ab.build_user_message(task, style=style) == "Body."


# ---------------------------------------------------------------------------
# build_user_message — coder style (code fences)
# ---------------------------------------------------------------------------


def test_build_user_message_coder_single_context_file_matches_old_inline_format(fake_project):
    ctx = fake_project / "context" / "foo.py"
    # Write without a trailing newline so the format string's `\n` is the
    # only one between content and the closing fence.
    ctx.write_text("print('hi')", encoding="utf-8")

    task = _make_task("Implement Y.", context_files=[str(ctx)])
    result = ab.build_user_message(task, style="coder", use_rag=False)

    # Byte-for-byte the same string the old inline code in agent_coder.py
    # produced (before the boilerplate refactor).
    expected = "### foo.py\n```\nprint('hi')\n```\n\n---\n\nImplement Y."
    assert result == expected


def test_build_user_message_coder_two_context_files_joined_by_blank_line(fake_project):
    a = fake_project / "context" / "a.py"
    b = fake_project / "context" / "b.py"
    a.write_text("AAA", encoding="utf-8")
    b.write_text("BBB", encoding="utf-8")

    task = _make_task("Do it.", context_files=[str(a), str(b)])
    result = ab.build_user_message(task, style="coder", use_rag=False)

    expected = (
        "### a.py\n```\nAAA\n```"
        "\n\n"
        "### b.py\n```\nBBB\n```"
        "\n\n---\n\nDo it."
    )
    assert result == expected


def test_build_user_message_coder_with_rag_prepends_kb_block(fake_project):
    """When use_rag=True and rag_query returns a hit, the KB block sits inside
    the body (after the context-files separator), matching the old behaviour
    where ``inject_rag_context(task["body"])`` ran before context prepending."""
    ctx = fake_project / "context" / "foo.py"
    ctx.write_text("CODE", encoding="utf-8")

    task = _make_task("Body.", context_files=[str(ctx)])
    with patch.object(ri, "rag_query", return_value="## doc_a\n\nhit"):
        result = ab.build_user_message(task, style="coder", use_rag=True)

    # Context block first, then `\n\n---\n\n`, then KB block on the body side.
    assert result.startswith("### foo.py\n```\nCODE\n```\n\n---\n\n## Knowledge Base Context\n")
    assert result.endswith("Body.")


def test_build_user_message_coder_with_rag_unavailable_is_noop(fake_project):
    """If the RAG API is offline, behaviour matches use_rag=False."""
    ctx = fake_project / "context" / "foo.py"
    ctx.write_text("CODE", encoding="utf-8")

    task = _make_task("Body.", context_files=[str(ctx)])
    with patch.object(ri, "rag_query", return_value="Knowledge base unavailable"):
        with_rag = ab.build_user_message(task, style="coder", use_rag=True)
    without_rag = ab.build_user_message(task, style="coder", use_rag=False)
    assert with_rag == without_rag


def test_build_user_message_coder_rag_char_limit_is_forwarded(fake_project):
    """``rag_char_limit`` flows through to ``inject_rag_context``."""
    task = _make_task("x" * 5000)
    with patch.object(ri, "rag_query", return_value="## doc\nhit") as mock:
        ab.build_user_message(task, style="coder", use_rag=True, rag_char_limit=120)
    args, _ = mock.call_args
    assert len(args[0]) == 120


# ---------------------------------------------------------------------------
# build_user_message — research style (no fences, --- separators)
# ---------------------------------------------------------------------------


def test_build_user_message_research_single_context_file(fake_project):
    ctx = fake_project / "context" / "notes.md"
    ctx.write_text("Some prose.", encoding="utf-8")

    task = _make_task("Summarise.", context_files=[str(ctx)])
    result = ab.build_user_message(task, style="research", use_rag=False)

    # Byte-for-byte the same string the old inline code in agent_research.py
    # produced (before the boilerplate refactor).
    expected = "### notes.md\n\nSome prose.\n\n---\n\nSummarise."
    assert result == expected


def test_build_user_message_research_two_files_joined_by_hr(fake_project):
    a = fake_project / "context" / "a.md"
    b = fake_project / "context" / "b.md"
    a.write_text("AAA", encoding="utf-8")
    b.write_text("BBB", encoding="utf-8")

    task = _make_task("Compare.", context_files=[str(a), str(b)])
    result = ab.build_user_message(task, style="research", use_rag=False)

    expected = (
        "### a.md\n\nAAA"
        "\n\n---\n\n"
        "### b.md\n\nBBB"
        "\n\n---\n\nCompare."
    )
    assert result == expected
    # And — critically — no code fences anywhere (research uses prose context).
    assert "```" not in result


# ---------------------------------------------------------------------------
# build_user_message — claude-code style ("Context:" prefix, no fences)
# ---------------------------------------------------------------------------


def test_build_user_message_claude_code_single_context_file(fake_project):
    ctx = fake_project / "context" / "thing.txt"
    ctx.write_text("contents", encoding="utf-8")

    task = _make_task("Do.", context_files=[str(ctx)])
    result = ab.build_user_message(task, style="claude-code", use_rag=False)

    # Byte-for-byte the same string the old inline code in agent_claude_code.py
    # produced (before the boilerplate refactor).
    expected = "### Context: thing.txt\n\ncontents\n\n---\n\nDo."
    assert result == expected


def test_build_user_message_claude_code_two_files_joined_by_blank_line(fake_project):
    a = fake_project / "context" / "a.txt"
    b = fake_project / "context" / "b.txt"
    a.write_text("AAA", encoding="utf-8")
    b.write_text("BBB", encoding="utf-8")

    task = _make_task("Body.", context_files=[str(a), str(b)])
    result = ab.build_user_message(task, style="claude-code", use_rag=False)

    expected = (
        "### Context: a.txt\n\nAAA"
        "\n\n"
        "### Context: b.txt\n\nBBB"
        "\n\n---\n\nBody."
    )
    assert result == expected


# ---------------------------------------------------------------------------
# build_user_message — unreadable context files are skipped
# ---------------------------------------------------------------------------


def test_build_user_message_skips_unreadable_context_files(fake_project):
    """When ``safe_read_context`` returns None (missing/traversal/etc.), the
    file is silently skipped — matches the old inline behaviour."""
    real = fake_project / "context" / "ok.py"
    real.write_text("OK", encoding="utf-8")
    missing = fake_project / "context" / "missing.py"  # not created

    task = _make_task("Body.", context_files=[str(missing), str(real)])
    result = ab.build_user_message(task, style="coder", use_rag=False)

    # Only the real file appears; no empty fence block, no error.
    assert "missing.py" not in result
    assert "ok.py" in result
    assert result.endswith("Body.")


def test_build_user_message_all_context_files_unreadable_returns_body_only(fake_project):
    """If every context file is unreadable, we fall back to just the body —
    no dangling separator."""
    missing = fake_project / "context" / "nope.py"

    task = _make_task("Body.", context_files=[str(missing)])
    result = ab.build_user_message(task, style="coder", use_rag=False)
    assert result == "Body."


# ---------------------------------------------------------------------------
# log_tokens_safe
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal stub matching ``OllamaClient.last_token_counts`` shape."""

    def __init__(self, counts):
        self.last_token_counts = counts


def test_log_tokens_safe_with_client_counts(fake_project):
    client = _FakeClient({"prompt": 100, "completion": 50})
    ab.log_tokens_safe("coder", "task_20260101_120000_000001", client)

    log_file = fake_project / "logs" / "coder" / "tokens.jsonl"
    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["prompt"] == 100
    assert entry["completion"] == 50


def test_log_tokens_safe_with_fallback_for_claude_code(fake_project):
    """The claude-code CLI doesn't report tokens; we log (0, word_count)."""
    response = "one two three four five"
    ab.log_tokens_safe(
        "claude-code",
        "task_20260101_120000_000001",
        response,
        fallback_completion=len(response.split()),
    )

    log_file = fake_project / "logs" / "claude-code" / "tokens.jsonl"
    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["prompt"] == 0
    assert entry["completion"] == 5


def test_log_tokens_safe_silent_noop_when_no_counts_and_no_fallback(fake_project):
    """If the client has no last_token_counts and there's no fallback, the
    call is a silent no-op (matches today's behaviour when Ollama returned
    an empty response and never populated counts)."""
    client = _FakeClient(None)
    ab.log_tokens_safe("coder", "task_20260101_120000_000001", client)

    log_file = fake_project / "logs" / "coder" / "tokens.jsonl"
    assert not log_file.exists()


def test_log_tokens_safe_silent_noop_with_partial_counts(fake_project):
    """``last_token_counts`` missing one of the keys → no-op rather than KeyError.
    Defensive: today's agents trust the dict is fully populated, but the helper
    should not be the one to crash."""
    client = _FakeClient({"prompt": 100})  # missing "completion"
    ab.log_tokens_safe("coder", "task_20260101_120000_000001", client)

    log_file = fake_project / "logs" / "coder" / "tokens.jsonl"
    assert not log_file.exists()


def test_log_tokens_safe_prefers_client_counts_over_fallback(fake_project):
    """If both a client with counts AND a fallback are supplied, the real counts
    win — fallback is only a backup."""
    client = _FakeClient({"prompt": 100, "completion": 50})
    ab.log_tokens_safe(
        "coder", "task_20260101_120000_000001", client, fallback_completion=999
    )

    log_file = fake_project / "logs" / "coder" / "tokens.jsonl"
    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["completion"] == 50  # real, not 999


# ---------------------------------------------------------------------------
# Parity check — output identical to the old inline code paths
# ---------------------------------------------------------------------------


def _old_coder_inline(task, *, rag_result):
    """Reproduce agent_coder.py's old inline context rendering exactly."""
    from shared.task_io import safe_read_context

    user_message = rag_result  # what inject_rag_context returned
    context_files = task["meta"].get("context_files", [])
    if context_files:
        context_content = []
        for cf in context_files:
            content = safe_read_context(cf)
            if content is not None:
                context_content.append(f"### {Path(cf).name}\n```\n{content}\n```")
        if context_content:
            user_message = "\n\n".join(context_content) + "\n\n---\n\n" + user_message
    return user_message


def _old_research_inline(task):
    """Reproduce agent_research.py's old inline context rendering exactly."""
    from shared.task_io import safe_read_context

    user_message = task["body"]
    context_files = task["meta"].get("context_files", [])
    if context_files:
        context_content = []
        for cf in context_files:
            content = safe_read_context(cf)
            if content is not None:
                context_content.append(f"### {Path(cf).name}\n\n{content}")
        if context_content:
            user_message = "\n\n---\n\n".join(context_content) + "\n\n---\n\n" + user_message
    return user_message


def _old_claude_code_inline(task):
    """Reproduce agent_claude_code.py's old inline context rendering exactly."""
    from shared.task_io import safe_read_context

    user_message = task["body"]
    context_files = task["meta"].get("context_files", [])
    if context_files:
        context_parts = []
        for cf in context_files:
            content = safe_read_context(cf)
            if content is not None:
                context_parts.append(f"### Context: {Path(cf).name}\n\n{content}")
        if context_parts:
            user_message = "\n\n".join(context_parts) + "\n\n---\n\n" + user_message
    return user_message


def test_parity_coder_no_rag(fake_project):
    a = fake_project / "context" / "a.py"
    b = fake_project / "context" / "b.py"
    a.write_text("AA\nlines\n", encoding="utf-8")
    b.write_text("BB", encoding="utf-8")

    task = _make_task("Body line 1\nline 2", context_files=[str(a), str(b)])
    new = ab.build_user_message(task, style="coder", use_rag=False)
    old = _old_coder_inline(task, rag_result=task["body"])  # use_rag=False → body untouched
    assert new == old


def test_parity_research(fake_project):
    a = fake_project / "context" / "a.md"
    b = fake_project / "context" / "b.md"
    a.write_text("Doc A.", encoding="utf-8")
    b.write_text("Doc B.", encoding="utf-8")

    task = _make_task("Compare A and B.", context_files=[str(a), str(b)])
    new = ab.build_user_message(task, style="research", use_rag=False)
    old = _old_research_inline(task)
    assert new == old


def test_parity_claude_code(fake_project):
    a = fake_project / "context" / "spec.txt"
    a.write_text("the spec", encoding="utf-8")

    task = _make_task("Do the thing.", context_files=[str(a)])
    new = ab.build_user_message(task, style="claude-code", use_rag=False)
    old = _old_claude_code_inline(task)
    assert new == old
