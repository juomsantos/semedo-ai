"""
agent_boilerplate.py — Shared boilerplate for worker agents (M6).

Before this existed, the four worker agents (coder, research, QA, claude-code)
each re-implemented load-system-prompt, load-context-files, and token-logging.
A change to any one (e.g. the ``safe_read_context`` rollout for C3) required
edits across four files. This module owns the canonical implementation.

Three exports:

  * ``load_system_prompt(agent_name)``     — read agents/<name>/system_prompt.md
  * ``build_user_message(task, *, style)`` — build the user message string
  * ``log_tokens_safe(agent, task_id, ...)`` — log token usage with graceful
    fallback for the claude-code CLI case (no token counts reported).

Behaviour-preserving: the three agents that use ``build_user_message`` produce
byte-identical output to their previous inline code (see
``tests/test_agent_boilerplate.py`` for the parity matrix).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from shared.logger import AgentLogger
from shared.rag_injection import inject_rag_context
from shared.task_io import PROJECT_ROOT, safe_read_context
from shared.token_logger import log_tokens

ContextStyle = Literal["coder", "research", "claude-code"]


def load_system_prompt(agent_name: str) -> str:
    """Return the contents of ``agents/<agent_name>/system_prompt.md`` (UTF-8).

    Raises ``FileNotFoundError`` if the file does not exist — callers should
    let this propagate so the agent fails its startup pre-flight rather than
    running headless with no system prompt.
    """
    path = PROJECT_ROOT / "agents" / agent_name / "system_prompt.md"
    return path.read_text(encoding="utf-8")


def build_user_message(
    task: dict,
    *,
    style: ContextStyle,
    use_rag: bool = False,
    rag_char_limit: int = 500,
    logger: Optional[AgentLogger] = None,
) -> str:
    """Build the user message for an LLM call.

    Combines, in order:

      1. RAG pre-injection on ``task["body"]`` — only when ``use_rag=True``.
         Coder uses this (no tool loop); research/QA use tool-mode RAG via
         ``rag_query`` in their ``chat_with_tools`` loop and pass
         ``use_rag=False``; claude-code never injects RAG.

      2. Context-file rendering — per-style format, preserving today's exact
         output byte-for-byte. The three formats are deliberately different
         (see each agent's system prompt in ``agents/<name>/system_prompt.md``):

           - ``"coder"``        → ``"### {name}\\n```\\n{content}\\n```"``
                                  joined by ``"\\n\\n"``. Code fences signal
                                  "this is verbatim source code in language X".
           - ``"research"``     → ``"### {name}\\n\\n{content}"`` joined by
                                  ``"\\n\\n---\\n\\n"``. No fences (context is
                                  prose); ``---`` is a markdown HR between
                                  documents.
           - ``"claude-code"``  → ``"### Context: {name}\\n\\n{content}"`` joined
                                  by ``"\\n\\n"``. Generic label, no fences
                                  (content type unknown).

         All three terminate with ``"\\n\\n---\\n\\n"`` before the body.

      3. The (possibly RAG-prefixed) task body.

    Validation context (M4) is NOT injected here. For coder/research/
    claude-code, ``task_io.create_task_file`` already prepended the
    ``## Validation Context`` section to ``task["body"]`` at task-creation
    time, so it arrives "for free" in the body. QA is the outlier — it builds
    its prompt from ``original_description`` (intentionally VC-free) and so
    re-injects via ``prepend_validation_context`` directly in
    ``agent_qa.review_with_llm``. QA does not call this helper.

    Args:
        task: Parsed task dict from ``read_task`` — must have ``"body"`` (str)
            and ``"meta"`` (dict, may include ``"context_files"``).
        style: Which agent's rendering format to use.
        use_rag: When ``True``, prepend a ``## Knowledge Base Context`` block
            via ``inject_rag_context``. Defaults to ``False``.
        rag_char_limit: Passed through to ``inject_rag_context`` — the number
            of chars from ``task["body"]`` to send as the RAG query.
        logger: Optional ``AgentLogger`` — forwarded to ``safe_read_context``
            so unreadable / rejected context files are surfaced in the agent
            log.

    Returns:
        The fully-assembled user message string.
    """
    body = task["body"]
    if use_rag:
        body = inject_rag_context(body, char_limit=rag_char_limit)

    context_files = task["meta"].get("context_files", []) or []
    if not context_files:
        return body

    parts: list[str] = []
    for cf in context_files:
        content = safe_read_context(cf, logger=logger)
        if content is None:
            continue
        name = Path(cf).name
        if style == "coder":
            parts.append(f"### {name}\n```\n{content}\n```")
        elif style == "research":
            parts.append(f"### {name}\n\n{content}")
        elif style == "claude-code":
            parts.append(f"### Context: {name}\n\n{content}")

    if not parts:
        return body

    if style == "research":
        joined = "\n\n---\n\n".join(parts)
    else:
        joined = "\n\n".join(parts)
    return joined + "\n\n---\n\n" + body


def log_tokens_safe(
    agent_name: str,
    task_id: str,
    client_or_response: Any,
    *,
    fallback_completion: Optional[int] = None,
) -> None:
    """Log token usage, gracefully handling the claude-code case (M7).

    Two call patterns:

      1. **OllamaClient**: pass ``client`` directly. The helper reads
         ``client.last_token_counts["prompt"]`` and ``["completion"]`` and
         forwards them to ``log_tokens``. Used by coder/research/QA.

      2. **No native counts (claude-code)**: pass any object (typically the
         response string) and the word-count proxy via
         ``fallback_completion=len(response.split())``. The helper logs
         ``(0, fallback_completion)``. Documented in the dashboard's Agent
         Stats tab as approximate (M7); replacing it with real Anthropic SDK
         counts is tracked separately.

    Silently no-ops if neither pattern matches — e.g. when Ollama returned an
    empty response and ``last_token_counts`` was never populated. This matches
    the previous behaviour: callers don't have to wrap every log site in a
    ``try``/``except``.
    """
    counts = getattr(client_or_response, "last_token_counts", None)
    if isinstance(counts, dict) and "prompt" in counts and "completion" in counts:
        log_tokens(agent_name, task_id, counts["prompt"], counts["completion"])
        return
    if fallback_completion is not None:
        log_tokens(agent_name, task_id, 0, fallback_completion)
        return
    # Neither pattern matched — silent no-op.
