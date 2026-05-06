"""
web_search.py — DuckDuckGo search wrapper for the research agent.

Returns a formatted string of results (title, URL, snippet) ready to be
injected into an LLM message as tool output.

Usage:
    from shared.web_search import web_search
    results = web_search("Python asyncio tutorial", max_results=5)
    print(results)  # formatted markdown string
"""

from typing import Optional


def web_search(query: str, max_results: int = 5) -> str:
    """
    Search DuckDuckGo and return formatted results as a markdown string.

    Args:
        query:       The search query.
        max_results: Maximum number of results to return (default 5).

    Returns:
        A markdown-formatted string listing each result as:
            ## 1. <title>
            URL: <url>
            <snippet>
        Or an error message string if the search fails.
    """
    try:
        from duckduckgo_search import DDGS  # old package name (duckduckgo-search)
    except ImportError:
        try:
            from ddgs import DDGS  # new package name (ddgs)
        except ImportError:
            return (
                "ERROR: search package is not installed. "
                "Run: pip install ddgs --break-system-packages"
            )

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"ERROR: Web search failed — {e}"

    if not results:
        return f"No results found for query: {query}"

    lines = [f"Search results for: **{query}**\n"]
    for i, r in enumerate(results, start=1):
        title = r.get("title", "(no title)")
        url = r.get("href", "")
        snippet = r.get("body", "").strip()
        lines.append(f"## {i}. {title}")
        if url:
            lines.append(f"URL: {url}")
        if snippet:
            lines.append(snippet)
        lines.append("")  # blank line between results

    return "\n".join(lines)
