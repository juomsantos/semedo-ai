"""
Behavioral error-path tests for the worker agents.

`test_agent_error_handling.py` proves *structurally* (via AST inspection) that
the error-handling patterns are present. This module proves them *behaviorally*
by actually driving the code with a failing/odd LLM client and asserting the
observable outcome:

  * coder / research move the task to ``failed/`` when the Ollama call raises
    ``OllamaError`` (no exception escapes ``process_task``);
  * QA's ``review_with_llm`` maps an ``OllamaError`` to a distinct ``ERROR``
    verdict (infrastructure failure, not a code verdict — no coder retry),
    maps an empty model response to ``FAIL``, and parses explicit PASS / FAIL
    verdicts.

The LLM is replaced with tiny stub clients — no network, no real Ollama.
"""

from __future__ import annotations

import pytest

from shared.ollama_client import OllamaError
from shared.task_io import create_task_file, read_task


class _Log:
    """Minimal AgentLogger stand-in."""

    def __init__(self):
        self.records = []

    def _add(self, lvl, m):
        self.records.append((lvl, str(m)))

    def info(self, m):
        self._add("info", m)

    def warning(self, m):
        self._add("warning", m)

    def error(self, m):
        self._add("error", m)

    def debug(self, m):
        self._add("debug", m)


class _RaisingClient:
    """Every LLM call raises OllamaError."""

    last_token_counts = {"prompt": 0, "completion": 0}

    def chat(self, *a, **k):
        raise OllamaError("simulated Ollama failure")

    def chat_with_tools(self, *a, **k):
        raise OllamaError("simulated Ollama failure")


class _CannedToolClient:
    """chat_with_tools always returns the same canned text result."""

    last_token_counts = {"prompt": 0, "completion": 0}

    def __init__(self, content):
        self._content = content

    def chat_with_tools(self, *a, **k):
        return {"type": "text", "content": self._content}


@pytest.fixture
def qa(monkeypatch):
    """agent_qa with its system-prompt load and token logging stubbed.

    ``review_with_llm`` only needs *a* system prompt string and must not write
    real token logs, so we patch the two module-level helpers directly. This
    keeps the test independent of ``PROJECT_ROOT`` and of any filesystem state
    left behind by other tests.
    """
    import agent_qa

    monkeypatch.setattr(agent_qa, "load_system_prompt", lambda name: "You are the QA reviewer.")
    monkeypatch.setattr(agent_qa, "log_tokens_safe", lambda *a, **k: None)
    return agent_qa


# ---------------------------------------------------------------------------
# coder / research — OllamaError must route the task to failed/
# ---------------------------------------------------------------------------


def test_coder_marks_task_failed_on_ollama_error(fake_project, monkeypatch):
    import agent_coder

    monkeypatch.setattr(agent_coder, "PROJECT_ROOT", fake_project)
    (fake_project / "agents" / "coder" / "system_prompt.md").write_text(
        "You are a coder.", encoding="utf-8"
    )
    # Coder uses pre-prompt RAG injection; stub it out so no network is touched.
    monkeypatch.setattr(
        "shared.agent_boilerplate.inject_rag_context",
        lambda body, char_limit=500: body,
    )

    task_path = create_task_file(
        inbox_path=fake_project / "agents" / "coder" / "inbox",
        task_type="code",
        description="write something",
        expected_output="code",
        assigned_to="coder",
        created_by="test",
    )
    task = read_task(task_path)

    agent_coder.process_task(task, _RaisingClient(), _Log())

    assert (fake_project / "failed" / task_path.name).exists()
    assert not (fake_project / "processing" / task_path.name).exists()


def test_research_marks_task_failed_on_ollama_error(fake_project, monkeypatch):
    import agent_research

    monkeypatch.setattr(agent_research, "PROJECT_ROOT", fake_project)
    (fake_project / "agents" / "research" / "system_prompt.md").write_text(
        "You research.", encoding="utf-8"
    )

    task_path = create_task_file(
        inbox_path=fake_project / "agents" / "research" / "inbox",
        task_type="research",
        description="research something",
        expected_output="a summary",
        assigned_to="research",
        created_by="test",
    )
    task = read_task(task_path)

    agent_research.process_task(task, _RaisingClient(), _Log())

    assert (fake_project / "failed" / task_path.name).exists()
    assert not (fake_project / "processing" / task_path.name).exists()


# ---------------------------------------------------------------------------
# QA — review_with_llm verdict behavior
# ---------------------------------------------------------------------------


def test_qa_review_returns_error_on_ollama_error(qa):
    # An OllamaError is an INFRASTRUCTURE failure of QA's own LLM call, not a
    # code-quality verdict. It must map to a distinct ERROR verdict so the caller
    # marks the task failed (orchestrator-managed retry) rather than spawning a
    # coder retry as if the code under review had failed.
    review = qa.review_with_llm(
        "the task", "the code", qa._NOT_EXECUTED, [], _RaisingClient(), _Log(), "t1"
    )
    assert review["verdict"] == "ERROR"
    assert review["verdict"] != "FAIL"


def test_qa_review_returns_pass_on_pass_verdict(qa):
    client = _CannedToolClient("VERDICT: PASS")
    review = qa.review_with_llm(
        "the task", "the code", qa._NOT_EXECUTED, [], client, _Log(), "t2"
    )
    assert review["verdict"] == "PASS"


def test_qa_review_returns_fail_with_feedback_on_fail_verdict(qa):
    client = _CannedToolClient("VERDICT: FAIL\nFEEDBACK: missing an edge case")
    review = qa.review_with_llm(
        "the task", "the code", qa._NOT_EXECUTED, [], client, _Log(), "t3"
    )
    assert review["verdict"] == "FAIL"
    assert "edge case" in review["feedback"]


def test_qa_review_returns_fail_on_empty_response(qa):
    client = _CannedToolClient("")  # model keeps returning empty text
    review = qa.review_with_llm(
        "the task", "the code", qa._NOT_EXECUTED, [], client, _Log(), "t4"
    )
    assert review["verdict"] == "FAIL"
