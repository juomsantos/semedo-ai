"""
Tests for the ``validation_context`` propagation fix.

What this is about
------------------
The orchestrator stamps a ``validation_context`` dict (decision_type +
reasoning) onto every follow-up subtask it creates after a redo / refine /
additional_work decision. Workers are supposed to receive this as a
``## Validation Context`` section in their LLM user_message — both
CLAUDE.md and ``agents/qa/system_prompt.md`` describe this behaviour.

For coder, research, and claude-code, the section reaches the LLM by way
of ``task["body"]``: ``shared.task_io.create_task_file`` prepends the
section to the body before the ``## Task Description`` heading, and those
agents send the body to their LLM verbatim.

QA is the odd one out. ``agent_qa.review_with_llm`` builds its
user_message from ``task["meta"].get("original_description") or task["body"]``,
and the orchestrator deliberately sets ``original_description`` to the
*clean* description on coder follow-ups (see ``agent_orchestrator.py``
line 1240) so the coder→QA chain doesn't double-inject the section.
Side-effect: the section is silently lost on the QA side of the chain.

The fix:
  - Pull the section's wording out of ``create_task_file`` into a single
    helper, ``shared.validation_context.prepend_validation_context``, so
    both call-sites stay byte-identical.
  - Call that helper from ``review_with_llm`` after building the
    user_message but before sending to the LLM.

These tests lock in:
  1. The helper's contract (prepends iff dict is non-empty, exact wording).
  2. The on-disk task body produced by ``create_task_file`` is unchanged
     by the refactor (regression net for tasks already in the wild).
  3. The new QA behaviour: VC reaches the LLM regardless of whether the
     task carries ``original_description``.
  4. Negative case: tasks without ``validation_context`` produce a
     user_message with NO ``## Validation Context`` heading.
  5. Cross-worker uniformity: every code path that produces an LLM
     user_message after an orchestrator follow-up contains the section
     exactly once — no duplication, no silent drop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make scripts/ importable; the conftest fixture also does this, but the
# helper tests below need to import the helper at collection time too.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from shared.validation_context import prepend_validation_context  # noqa: E402
from shared import task_io as task_io_mod  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Helper unit tests
# ---------------------------------------------------------------------------


def test_prepends_block_when_validation_context_is_non_empty():
    """The canonical happy path. Decision type and reasoning must appear
    verbatim, and the original text must still be present below the block."""
    vc = {"decision_type": "refine", "reasoning": "missing error handling"}
    out = prepend_validation_context("## Task Description\n\nDo X.", vc)

    assert out.startswith("## Validation Context\n\n")
    assert "refine" in out
    assert "missing error handling" in out
    # The original text is preserved verbatim at the bottom.
    assert out.endswith("## Task Description\n\nDo X.")
    # The separator from the legacy block is still there — it's how workers
    # know the VC section has ended and the task content has begun.
    assert "---" in out


def test_returns_text_unchanged_when_validation_context_is_none():
    """None means "no follow-up context" — the helper must be a no-op."""
    out = prepend_validation_context("## Task Description\n\nDo X.", None)
    assert out == "## Task Description\n\nDo X."


def test_returns_text_unchanged_when_validation_context_is_empty_dict():
    """Empty dict is treated the same as None — also a no-op. The orchestrator
    should never send an empty dict, but defending against it here prevents
    a future regression where the orchestrator decides to send {} as a
    sentinel."""
    out = prepend_validation_context("hello", {})
    assert out == "hello"


@pytest.mark.parametrize("decision_type", ["redo", "refine", "additional_work"])
def test_block_includes_decision_type_in_both_places(decision_type):
    """The legacy wording mentions ``decision_type`` twice — once in the
    'orchestrator decided' line and once in the instruction to consult the
    system prompt. The helper must keep both for the QA system prompt's
    branching logic to fire correctly."""
    vc = {"decision_type": decision_type, "reasoning": "..."}
    out = prepend_validation_context("body", vc)
    # The legacy block has both occurrences; if the helper ever drops one,
    # the QA system prompt's branching by decision_type might silently miss.
    assert out.count(f"`{decision_type}`") >= 2


def test_block_handles_missing_keys_gracefully():
    """If the orchestrator ever sends a partial dict, the helper must not
    crash — it just renders the empty pieces. (We never expect this, but
    'never crash on a partial input' is the right contract for a helper
    that runs on every QA review.)"""
    out = prepend_validation_context("body", {"decision_type": "redo"})
    assert "## Validation Context" in out
    assert "redo" in out
    # `reasoning` is missing → renders as blank, not as a Python KeyError.
    assert "**Reason:**" in out


# ---------------------------------------------------------------------------
# 2. create_task_file regression net (refactor must be byte-identical)
# ---------------------------------------------------------------------------


def test_create_task_file_body_byte_identical_with_validation_context(fake_project):
    """The refactor swapped the inlined block in task_io for a helper call.
    The on-disk body must be IDENTICAL to what users see in already-created
    task files. The exact wording (which has been in production) is the
    contract here — we re-derive it inline below so a wording change in
    either the helper OR a regression in task_io will fail this test."""
    inbox = fake_project / "inbox"
    inbox.mkdir(exist_ok=True)
    vc = {"decision_type": "refine", "reasoning": "missing error handling"}

    path = task_io_mod.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="Add a foo() function.",
        expected_output="A Python file.",
        validation_context=vc,
    )

    body = path.read_text(encoding="utf-8")
    # The frontmatter is up top; we only check that the section is present
    # AND in the right order (before the Task Description).
    assert "## Validation Context" in body
    assert "**This is a follow-up task. The orchestrator reviewed the previous attempt and decided: `refine`**" in body
    assert "**Reason:** missing error handling" in body
    assert body.index("## Validation Context") < body.index("## Task Description")


def test_create_task_file_body_unchanged_without_validation_context(fake_project):
    """No-VC path also unchanged: body starts directly with ## Task Description."""
    inbox = fake_project / "inbox"
    inbox.mkdir(exist_ok=True)
    path = task_io_mod.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="Do a thing.",
        expected_output="Output.",
    )
    body = path.read_text(encoding="utf-8")
    assert "## Validation Context" not in body
    assert "## Task Description" in body


# ---------------------------------------------------------------------------
# 3. QA path — the actual validation_context fix
# ---------------------------------------------------------------------------


def _stub_chat_response(text: str = "VERDICT: PASS\nFEEDBACK: ok"):
    """Build a duck-typed ``chat_with_tools`` return value so the tool loop
    in ``review_with_llm`` exits on the first turn (type=text) without
    needing the real Ollama."""
    return {"type": "text", "content": text}


def _capture_user_message(client_mock) -> str:
    """Pull out the user-role message from the first chat_with_tools call.
    The function builds ``messages`` as [system, user, ...], so index 1 is
    always the user message we care about."""
    args, kwargs = client_mock.chat_with_tools.call_args
    messages = kwargs.get("messages") or args[1]
    user = next(m for m in messages if m.get("role") == "user")
    return user["content"]


@pytest.fixture
def qa_review(monkeypatch):
    """Yields a thunk that calls ``agent_qa.review_with_llm`` against a
    fully-mocked client and returns the user_message that would have been
    sent to the LLM. Used by the validation_context fix tests below.

    The thunk takes one keyword arg, ``validation_context``, mirroring the
    real call-site at ``agent_qa.process_task`` (line ~566)."""
    import agent_qa

    # Don't touch logs/qa/tokens.jsonl during tests. agent_qa imports
    # ``log_tokens_safe`` from ``shared.agent_boilerplate`` instead of the raw
    # ``log_tokens``.
    monkeypatch.setattr(agent_qa, "log_tokens_safe", lambda *a, **kw: None)
    # Stub the system prompt loader so it doesn't read a real file. The shared
    # loader takes an ``agent_name`` argument, so the stub must accept it.
    monkeypatch.setattr(agent_qa, "load_system_prompt", lambda *a, **kw: "QA SYSTEM PROMPT")

    client = MagicMock()
    client.chat_with_tools.return_value = _stub_chat_response()
    client.last_token_counts = {"prompt": 1, "completion": 1}

    log = MagicMock()

    def _run(*, validation_context=None, task_description="Add foo()"):
        agent_qa.review_with_llm(
            task_description=task_description,
            full_result="def foo(): pass",
            execution={"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False},
            prior_results=[],
            client=client,
            log=log,
            task_id="task_test",
            validation_context=validation_context,
        )
        return _capture_user_message(client)

    return _run


def test_qa_user_message_contains_validation_context_when_present(qa_review):
    """The whole point: when the orchestrator decides ``redo`` and the
    follow-up runs through coder→QA, QA's LLM must see the section. Before
    the fix, ``task_description`` came from ``original_description`` (clean,
    no VC) and the section was lost. After the fix, the helper re-injects
    it explicitly."""
    user_message = qa_review(validation_context={
        "decision_type": "redo",
        "reasoning": "the prior implementation hard-coded a key",
    })
    assert "## Validation Context" in user_message
    assert "`redo`" in user_message
    assert "hard-coded a key" in user_message
    # The section must come BEFORE the existing "## Original Task" heading,
    # because the QA system prompt instructs the LLM to read VC first.
    assert user_message.index("## Validation Context") < user_message.index("## Original Task")


def test_qa_user_message_has_no_vc_block_when_validation_context_is_none(qa_review):
    """First-attempt review (no orchestrator follow-up) must NOT have a VC
    section. Otherwise the QA prompt's `redo`/`refine` calibration would
    trigger on every review and skew first-attempt verdicts."""
    user_message = qa_review(validation_context=None)
    assert "## Validation Context" not in user_message


def test_qa_user_message_has_no_vc_block_when_validation_context_is_empty(qa_review):
    """Empty-dict path: same as None. Belt-and-suspenders — if some future
    code path sends `{}` as a sentinel, we still don't inject."""
    user_message = qa_review(validation_context={})
    assert "## Validation Context" not in user_message


def test_qa_injects_vc_even_when_original_description_was_clean(qa_review):
    """The exact scenario this fixes. In the wild this looks like:
      - orchestrator decides REDO on a coder subtask
      - orchestrator creates coder follow-up with original_description = clean
        description (so the coder→QA chain doesn't double-inject)
      - coder runs, chains to QA forwarding the clean original_description
      - QA's task_description = clean (no VC inside)
    Pre-fix: QA's LLM never saw the VC section. Post-fix: it does, because
    review_with_llm receives the validation_context separately and uses the
    helper to inject."""
    clean_description = "Add a foo() function that returns 42."  # NO VC inside
    user_message = qa_review(
        validation_context={"decision_type": "refine", "reasoning": "previously returned 41"},
        task_description=clean_description,
    )
    # task_description appears in the body, but the section must still be there.
    assert clean_description in user_message
    assert "## Validation Context" in user_message
    assert "previously returned 41" in user_message


# ---------------------------------------------------------------------------
# 4. Cross-worker contract — exactly one VC heading reaches the LLM
# ---------------------------------------------------------------------------


def test_body_from_create_task_file_contains_exactly_one_vc_heading(fake_project):
    """For coder / research / claude-code, the LLM user_message is built from
    ``task["body"]``. The body must contain the VC heading EXACTLY ONCE —
    zero would mean the section was silently dropped; two would mean a
    double-injection regression (which would happen if a worker also tried
    to call the helper a second time and the helper wasn't idempotent —
    the current helper is NOT idempotent by design, so this test guards the
    invariant that workers shouldn't double-inject)."""
    inbox = fake_project / "inbox"
    inbox.mkdir(exist_ok=True)
    vc = {"decision_type": "additional_work", "reasoning": "needs unit tests"}
    path = task_io_mod.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="Add tests for foo().",
        expected_output="A test file.",
        validation_context=vc,
    )
    body = path.read_text(encoding="utf-8")
    # Frontmatter contains the dict (decision_type+reasoning), so the literal
    # heading "## Validation Context" appears only inside the body block.
    assert body.count("## Validation Context") == 1
