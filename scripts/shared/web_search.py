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
            content = content.strip()
        lines.append(f"## {i}. {title}")
        if url:
            lines.append(f"URL: {url}")
        if content:
            lines.append(content)
        lines.append("")

    return "\n".join(lines)


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
            content = content.strip()
    except Exception as e:
        return f"ERROR: Web fetch failed — {e}"

    return f"# {title}\nURL: {url}\n\n{content}"
