"""
web_search.py — Ollama web search and web fetch wrappers.

Delegates to the ollama Python library's web_search() and web_fetch() functions.
The API key (OLLAMA_API_KEY) must be set before the ollama library is imported —
this is handled by ollama_client.py, which is always imported first by every
agent script and sets the env var before `import ollama` runs.

Both functions are designed to be passed directly as native tools to the ollama
library's chat() call — their type annotations and docstrings are used to
auto-generate the tool JSON schema.

Usage:
    from shared.web_search import web_search, web_fetch
    result = web_search("Python asyncio tutorial")
    page   = web_fetch("https://docs.python.org/3/library/asyncio.html")
"""

import ollama as _ollama

# Cap the size of web content fed back into the model. web_fetch returns the FULL
# page body and web_search can return many snippets; left unbounded, a single
# large page can overflow the model's context window — a 32k-token context once
# saw an 86k-token prompt and Ollama returned a 400 exceed_context_size error.
# These caps bound any single tool result. Sized in characters (~4 chars/token):
# 8000 chars ≈ ~2k tokens per fetched page, leaving room for several tool calls
# plus the system prompt and task within a 32k window.
MAX_FETCH_CONTENT_CHARS = 8000
MAX_SEARCH_RESULT_CHARS = 1200   # per individual search result snippet
MAX_SEARCH_TOTAL_CHARS = 6000    # backstop on a whole web_search() return (many results)


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` capped at ``limit`` chars with an explicit truncation marker."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n\n[... truncated {dropped} characters to fit the model context window ...]"


def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web for current information, facts, documentation, or recent events.

    Use this when you need information that may be outside your training data,
    want to verify a fact, or need the latest documentation or news.

    Args:
        query:       A concise, specific search query.
        max_results: Maximum number of results to return (default 5, max 10).

    Returns:
        A markdown-formatted string listing each result as:
            ## 1. <title>
            URL: <url>
            <content snippet>
        Or an error message string if the search fails.
    """
    try:
        response = _ollama.web_search(query, max_results=max_results)
        results = getattr(response, "results", None) or response.get("results", [])
    except Exception as e:
        return f"ERROR: Web search failed — {e}"

    if not results:
        return f"No results found for query: {query!r}"

    lines = [f"Search results for: **{query}**\n"]
    for i, r in enumerate(results, start=1):
        title   = getattr(r, "title",   None) or (r.get("title",   "(no title)") if isinstance(r, dict) else "(no title)")
        url     = getattr(r, "url",     None) or (r.get("url",     "")           if isinstance(r, dict) else "")
        content = getattr(r, "content", None) or (r.get("content", "")           if isinstance(r, dict) else "")
        if content:
            content = _truncate(content.strip(), MAX_SEARCH_RESULT_CHARS)
        lines.append(f"## {i}. {title}")  # noqa: keep result layout
        if url:
            lines.append(f"URL: {url}")
        if content:
            lines.append(content)
        lines.append("")

    return _truncate("\n".join(lines), MAX_SEARCH_TOTAL_CHARS)


def web_fetch(url: str) -> str:
    """
    Fetch the full content of a specific web page by URL.

    Use this after a web_search to read the full content of a promising result,
    or when you already know the exact URL you need.

    Args:
        url: The full URL of the page to fetch (must include https://).

    Returns:
        A markdown-formatted string with the page title and main content.
        Or an error message string if the fetch fails.
    """
    try:
        response = _ollama.web_fetch(url)
        title   = getattr(response, "title",   None) or (response.get("title",   "(no title)") if isinstance(response, dict) else "(no title)")
        content = getattr(response, "content", None) or (response.get("content", "")           if isinstance(response, dict) else "")
        if content:
            content = _truncate(content.strip(), MAX_FETCH_CONTENT_CHARS)
    except Exception as e:
        return f"ERROR: Web fetch failed — {e}"

    return f"# {title}\nURL: {url}\n\n{content}"
