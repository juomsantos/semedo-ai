"""
rag_injection.py — Shared "RAG pre-prompt injection" helper.

There are two ways agents use the RAG knowledge base:

  1. **Tool mode** — the agent's chat loop exposes ``rag_query`` as a callable
     and the model decides when (and whether) to call it. Used by
     ``agent_research.py`` and ``agent_qa.py``, which already have a
     ``chat_with_tools`` loop.

  2. **Pre-prompt injection** — the agent calls ``rag_query`` once *before*
     building the user message and prepends the result as a
     ``## Knowledge Base Context`` section. Used by ``agent_coder.py`` and
     ``agent_orchestrator.py``, which call ``client.chat`` (no tool loop)
     and so would otherwise have no way to consult the knowledge base.

This module owns the canonical implementation of pre-prompt injection — both
the query truncation and the "is this a useful result?" filter. Before this
existed the same block was copy-pasted byte-for-byte in two agents; any change
to the prefix string or the filter logic had to be made in both places.

Usage:

    from shared.rag_injection import inject_rag_context

    user_message = inject_rag_context(task["body"])
    # If the RAG API returned useful hits, user_message now starts with
    # "## Knowledge Base Context\\n...". Otherwise it is unchanged.
"""

from __future__ import annotations

from shared.rag_tool import rag_query

# Prefixes returned by ``rag_query`` when the result is not useful for the
# downstream LLM. ``rag_query`` deliberately returns plain strings (not
# exceptions) so the tool loop in research/QA can keep going — but for
# pre-prompt injection we want to drop these silently so we don't confuse the
# model with "Knowledge base unavailable" noise it can't act on.
_NON_USEFUL_PREFIXES = (
    "Knowledge base unavailable",
    "Knowledge base error",
    "No results found",
)

# Truncate the query at this many characters before sending to the RAG API.
# Embedding latency scales with input length; the first ~500 chars are nearly
# always enough to surface the right neighbours.
DEFAULT_QUERY_CHAR_LIMIT = 500


def inject_rag_context(task_body: str, char_limit: int = DEFAULT_QUERY_CHAR_LIMIT) -> str:
    """Return ``task_body`` with a ``## Knowledge Base Context`` section prepended
    when the RAG API has relevant hits; otherwise return ``task_body`` unchanged.

    The RAG API is queried with the first ``char_limit`` characters of
    ``task_body``. If the API is unavailable, errored, or returns no results,
    the task body is returned unchanged — the agent's behaviour with the
    RAG API offline is identical to its behaviour without the injection.

    Args:
        task_body: Original task body. May contain markdown, frontmatter has
            already been stripped by the caller.
        char_limit: Maximum number of characters from ``task_body`` to send
            as the RAG query (defaults to 500).

    Returns:
        The task body, optionally prefixed with a ``## Knowledge Base Context``
        block followed by a ``---`` separator.
    """
    if not task_body:
        return task_body

    rag_results = rag_query(task_body[:char_limit])
    if not rag_results:
        return task_body
    if rag_results.startswith(_NON_USEFUL_PREFIXES):
        return task_body

    return f"## Knowledge Base Context\n{rag_results}\n\n---\n\n{task_body}"
