"""
validation_context.py — Shared "Validation Context" pre-prompt injection.

When the orchestrator decides ``redo`` / ``refine`` / ``additional_work`` on a
parent task, it stamps a ``validation_context`` dict onto each follow-up
subtask so the worker knows why the previous attempt was inadequate. This
module is the single source of truth for the wording of the
``## Validation Context`` section that gets surfaced to the worker LLM.

There are two call-sites:

  1. ``task_io.create_task_file`` — writes the section into the on-disk task
     body before the ``## Task Description`` heading. The body is what most
     workers (coder, research, claude-code) feed straight into their LLM,
     and what the dashboard renders to a human, so the file-system view of
     a task is self-explanatory.

  2. ``agent_qa.review_with_llm`` — QA builds its LLM user_message from
     ``task["meta"].get("original_description") or task["body"]``.
     ``original_description`` is deliberately VC-free on coder-chained QA
     tasks (the orchestrator at agent_orchestrator.py:1240 strips it so the
     coder→QA chain doesn't double-inject the section). The side-effect is
     that QA's user_message can lose the VC entirely, even though the QA
     system prompt (agents/qa/system_prompt.md, lines 69-79) explicitly
     instructs the model to look for the section. QA therefore calls this
     helper to inject the section explicitly on the way to the LLM.

Having both call-sites use this function ensures the wording stays in lock-
step: change the section here and both the on-disk body and QA's LLM see
the new copy automatically.
"""

from __future__ import annotations

from typing import Optional, Mapping


def prepend_validation_context(
    text: str,
    validation_context: Optional[Mapping[str, str]],
) -> str:
    """Return ``text`` with a ``## Validation Context`` block prepended when
    ``validation_context`` is a non-empty mapping; otherwise return ``text``
    unchanged.

    The wording mirrors the inline block that ``task_io.create_task_file``
    used historically (kept byte-for-byte so existing task files on disk —
    and the dashboard rendering of them — continue to look identical after
    the refactor).

    Args:
        text: The message the worker would otherwise send to the LLM (for
            ``create_task_file`` this is just the description section; for
            QA this is the full pre-built ``## Original Task`` block).
        validation_context: Dict from the orchestrator with two keys:
            ``decision_type`` ("redo" / "refine" / "additional_work") and
            ``reasoning`` (the orchestrator's explanation). ``None`` or an
            empty mapping is a no-op.

    Returns:
        Either ``text`` unchanged or ``"<block>\\n\\n<text>"`` where ``<block>``
        ends with the ``---`` separator already present in the legacy
        wording.
    """
    if not validation_context:
        return text

    decision_type = validation_context.get("decision_type", "")
    reasoning = validation_context.get("reasoning", "")
    context_block = (
        f"## Validation Context\n\n"
        f"**This is a follow-up task. The orchestrator reviewed the previous attempt and decided: `{decision_type}`**\n\n"
        f"**Reason:** {reasoning}\n\n"
        f"Read the Validation Feedback Context section in your system prompt to understand "
        f"how to adjust your approach for a `{decision_type}` request.\n\n"
        f"---\n\n"
    )
    return context_block + text
