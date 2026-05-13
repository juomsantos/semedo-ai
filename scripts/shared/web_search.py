"""
web_search.py — Ollama web search and web fetch wrappers.

Uses Ollama's cloud API (https://ollama.com/api/web_search and /api/web_fetch).
Requires an Ollama API key configured in config.json under web_search.ollama_api_key.

Both functions are designed to be passed directly as native tools to the ollama
library's chat() call — their type annotations and docstrings are used to
auto-generate the tool JSON schema.

Usage:
    from shared.web_search import web_search, web_fetch
    result = web_search("Python asyncio tutorial")
    page   = web_fetch("https://docs.python.org/3/library/asyncio.html")
"""

import os
import requests

# Load API key from config and expose via OLLAMA_API_KEY env var
# (the ollama library reads this env var for cloud API calls)
try:
    from shared.config import load_config
    _api_key = load_config().web_search_api_key()
    if _api_key:
        os.environ["OLLAMA_API_KEY"] = _api_key
except Exception:
    _api_key = os.environ.get("OLLAMA_API_KEY", "")

_SEARCH_URL = "https://ollama.com/api/web_search"
_FETCH_URL  = "https://ollama.com/api/web_fetch"
_TIMEOUT_SEARCH = 15  # seconds
_TIMEOUT_FETCH  = 20


def _api_key_or_error() -> str | None:
    """Return the API key, or None if not configured."""
    return os.environ.get("OLLAMA_API_KEY", "") or None


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
    api_key = _api_key_or_error()
    if not api_key:
        return (
            "ERROR: OLLAMA_API_KEY is not configured. "
            "Add 'web_search.ollama_api_key' to config.json."
        )

    try:
        resp = requests.post(
            _SEARCH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "max_results": max_results},
            timeout=_TIMEOUT_SEARCH,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return f"ERROR: Web search timed out after {_TIMEOUT_SEARCH}s for query: {query!r}"
    except requests.exceptions.HTTPError as e:
        return f"ERROR: Web search HTTP error — {e}"
    except Exception as e:
        return f"ERROR: Web search failed — {e}"

    results = data.get("results", [])
    if not results:
        return f"No results found for query: {query!r}"

    lines = [f"Search results for: **{query}**\n"]
    for i, r in enumerate(results, start=1):
        title   = r.get("title", "(no title)")
        url     = r.get("url", "")
        content = r.get("content", "").strip()
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
    api_key = _api_key_or_error()
    if not api_key:
        return (
            "ERROR: OLLAMA_API_KEY is not configured. "
            "Add 'web_search.ollama_api_key' to config.json."
        )

    try:
        resp = requests.post(
            _FETCH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": url},
            timeout=_TIMEOUT_FETCH,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return f"ERROR: Web fetch timed out after {_TIMEOUT_FETCH}s for URL: {url}"
    except requests.exceptions.HTTPError as e:
        return f"ERROR: Web fetch HTTP error — {e}"
    except Exception as e:
        return f"ERROR: Web fetch failed — {e}"

    title   = data.get("title", "(no title)")
    content = data.get("content", "").strip()

    return f"# {title}\nURL: {url}\n\n{content}"
