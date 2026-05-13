"""
web_search.py — Ollama web search and web fetch wrappers.

Delegates to the ollama Python library's web_search() and web_fetch() functions,
which call Ollama's cloud API using the correct versioned endpoints internally.
Requires an Ollama API key configured in config.json under web_search.ollama_api_key
(exposed as the OLLAMA_API_KEY environment variable, which the library reads).

Both functions are designed to be passed directly as native tools to the ollama
library's chat() call — their type annotations and docstrings are used to
auto-generate the tool JSON schema.

Usage:
    from shared.web_search import web_search, web_fetch
    result = web_search("Python asyncio tutorial")
    page   = web_fetch("https://docs.python.org/3/library/asyncio.html")
"""

import os

# Load API key from config and expose via OLLAMA_API_KEY env var
# (the ollama library reads this env var for cloud API calls)
try:
    from shared.config import load_config
    _api_key = load_config().web_search_api_key()
    if _api_key:
        os.environ["OLLAMA_API_KEY"] = _api_key
except Exception:
    pass  # fall back to whatever is already in the environment

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
    if not os.environ.get("OLLAMA_API_KEY"):
        return (
            "ERROR: OLLAMA_API_KEY is not configured. "
            "Add 'web_search.ollama_api_key' to config.json."
        )

    try:
        response = _ollama.web_search(query, max_results=max_results)
        results = response.results if hasattr(response, "results") else response.get("results", [])
    except Exception as e:
        return f"ERROR: Web search failed — {e}"

    if not results:
        return f"No results found for query: {query!r}"

    lines = [f"Search results for: **{query}**\n"]
    for i, r in enumerate(results, start=1):
        # Support both object-style and dict-style results
        title   = getattr(r, "title",   None) or r.get("title",   "(no title)")
        url     = getattr(r, "url",     None) or r.get("url",     "")
        content = getattr(r, "content", None) or r.get("content", "")
        if content:
            content = content.strip()
        lines.append(f"## {i}. {title}")
        if url:
            lines.append(f"URL: {url}")
        if content:
            lines.append(content)
        lines.append("")  # blank line between results

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
    if not os.environ.get("OLLAMA_API_KEY"):
        return (
            "ERROR: OLLAMA_API_KEY is not configured. "
            "Add 'web_search.ollama_api_key' to config.json."
        )

    try:
        response = _ollama.web_fetch(url)
        title   = getattr(response, "title",   None) or response.get("title",   "(no title)")
        content = getattr(response, "content", None) or response.get("content", "")
        if content:
            content = content.strip()
    except Exception as e:
        return f"ERROR: Web fetch failed — {e}"

    return f"# {title}\nURL: {url}\n\n{content}"
