"""
rag_tool.py — Local knowledge-base query tool for AI Team agents.

Calls the RAG API (FastAPI/ChromaDB, running at localhost:8000 by default).
Returns a plain string so it can be used as a native Ollama tool alongside
web_search and web_fetch — the Ollama library introspects the type annotations
and docstring to auto-generate the JSON schema.

Usage:
    from shared.rag_tool import rag_query
    result = rag_query("how does the orchestrator validation loop work?")
"""

import requests as _requests
from shared.config import load_config as _load_config


def rag_query(query: str, top_k: int = 5) -> str:
    """
    Search the local knowledge base for relevant documentation or prior work.

    Use this BEFORE web_search when the information may already exist in the
    project knowledge base — past completed task results, uploaded reference
    docs, architecture notes, or code examples that were ingested previously.

    Prefer this over web_search for questions about this specific project,
    its architecture, prior implementations, or any document you have added
    to the knowledge base.

    Args:
        query:  A natural-language question or keyword description.
        top_k:  Number of results to return (default 5, max 20).

    Returns:
        A markdown-formatted string listing each matching document with its
        relevance score and a content snippet. Returns an error string (not
        an exception) if the knowledge base is unavailable.
    """
    try:
        base_url = _load_config().rag_api_url()
        resp = _requests.post(
            f"{base_url}/query",
            json={"query": query, "top_k": top_k},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except _requests.exceptions.ConnectionError:
        return "Knowledge base unavailable (RAG API not running — start with the scheduler or RUN_RAG_API.bat)"
    except Exception as e:
        return f"Knowledge base error: {e}"

    if not results:
        return f"No results found in knowledge base for: {query!r}"

    lines = [f"Knowledge base results for: **{query}**\n"]
    for i, res in enumerate(results, 1):
        score   = res.get("score", 0)
        doc_id  = res.get("document_id", "?")
        content = res.get("content", "").strip()[:400]
        lines.append(f"## {i}. {doc_id}  (score={score:.3f})")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)
