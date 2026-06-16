"""
Tests for the ``rag_api/`` package.

Focus areas:
  * ``vector_store._embed_content`` — the zero-vector embedding fallback now
    LOGS a warning on both failure paths (regression guard for the silent-
    degradation bug) and still never hard-crashes.
  * ``ollama_client.OllamaClient`` — ``embed`` success/error mapping and the
    cosine-similarity ``rerank``.
  * ``ingestion.TextChunker`` / ``DocumentLoader`` — chunking with overlap and
    chunk-dict shaping.

Import notes: rag_api modules import each other by bare name (e.g.
``from ollama_client import OllamaClient``), so we put ``rag_api/`` on
``sys.path``. ``vector_store`` imports ``chromadb`` at module load (a heavy
native dep the test suite doesn't install) but only *uses* it lazily, so we
stub ``chromadb`` in ``sys.modules`` — the tests here exercise ``_embed_content``
with an injected fake embed client and never touch ChromaDB.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RAG_API_DIR = REPO_ROOT / "rag_api"
if str(RAG_API_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_API_DIR))

# Stub the heavy native dep so `import chromadb` at vector_store load succeeds.
sys.modules.setdefault("chromadb", types.ModuleType("chromadb"))

import vector_store  # noqa: E402
import ollama_client as rag_ollama  # noqa: E402
import ingestion  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RaisingEmbed:
    def embed(self, content):
        raise Exception("Embedding error: request timed out")


class _EmptyEmbed:
    def embed(self, content):
        return []


class _GoodEmbed:
    def __init__(self, vec):
        self._vec = vec

    def embed(self, content):
        return list(self._vec)


def _store(client):
    return vector_store.ChromaDBPersistentClient(path="unused", ollama_client=client)


# ---------------------------------------------------------------------------
# vector_store._embed_content — finding 9 (zero-vector fallback is now logged)
# ---------------------------------------------------------------------------


def test_embed_fallback_on_exception_returns_zeros_and_warns(caplog):
    store = _store(_RaisingEmbed())
    with caplog.at_level(logging.WARNING):
        vec = store._embed_content("some content to embed")
    assert vec == [0.0] * vector_store._FALLBACK_EMBED_DIM
    assert "falling back to a zero vector" in caplog.text


def test_embed_fallback_on_empty_returns_zeros_and_warns(caplog):
    store = _store(_EmptyEmbed())
    with caplog.at_level(logging.WARNING):
        vec = store._embed_content("some content to embed")
    assert vec == [0.0] * vector_store._FALLBACK_EMBED_DIM
    assert "falling back to a zero vector" in caplog.text


def test_embed_success_returns_real_vector_without_warning(caplog):
    store = _store(_GoodEmbed([0.1, 0.2, 0.3]))
    with caplog.at_level(logging.WARNING):
        vec = store._embed_content("some content to embed")
    assert vec == [0.1, 0.2, 0.3]
    assert "zero vector" not in caplog.text


def test_embed_fallback_dim_is_named_constant():
    # The magic number is exposed as a named constant, not a literal.
    assert vector_store._FALLBACK_EMBED_DIM == 4096


# ---------------------------------------------------------------------------
# ollama_client.OllamaClient — embed + rerank
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_embed_returns_vector_on_200(monkeypatch):
    client = rag_ollama.OllamaClient(host="http://x", embed_model="m")
    monkeypatch.setattr(
        rag_ollama.requests, "post",
        lambda *a, **k: _Resp(200, {"embedding": [1.0, 2.0, 3.0]}),
    )
    assert client.embed("hello") == [1.0, 2.0, 3.0]


def test_embed_raises_on_http_error(monkeypatch):
    client = rag_ollama.OllamaClient()
    monkeypatch.setattr(
        rag_ollama.requests, "post",
        lambda *a, **k: _Resp(500, text="server boom"),
    )
    with pytest.raises(Exception):
        client.embed("hello")


def test_rerank_orders_by_cosine_similarity(monkeypatch):
    client = rag_ollama.OllamaClient()
    # query ~ [1,0]; doc "a" identical (sim 1.0), doc "b" orthogonal (sim 0.0).
    vectors = {"q": [1.0, 0.0], "a": [1.0, 0.0], "b": [0.0, 1.0]}
    monkeypatch.setattr(client, "embed", lambda text, model=None: vectors[text])

    ranked = client.rerank("q", [{"content": "b"}, {"content": "a"}])

    assert [d["content"] for d in ranked] == ["a", "b"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_rerank_empty_documents_returns_empty():
    assert rag_ollama.OllamaClient().rerank("q", []) == []


# ---------------------------------------------------------------------------
# ingestion.TextChunker / DocumentLoader
# ---------------------------------------------------------------------------


def test_chunk_short_text_is_single_chunk():
    chunker = ingestion.TextChunker(chunk_size=100, chunk_overlap=10)
    assert chunker.chunk("short") == ["short"]


def test_chunk_empty_text_is_empty_list():
    assert ingestion.TextChunker().chunk("") == []


def test_chunk_long_text_overlaps():
    text = "abcdefghijklmnopqrstuvwxy"  # 25 chars
    chunker = ingestion.TextChunker(chunk_size=10, chunk_overlap=3)
    chunks = chunker.chunk(text)
    assert len(chunks) > 1
    # The last `overlap` chars of one chunk are the first chars of the next.
    assert chunks[0][-3:] == chunks[1][:3]
    # Reassembling (dropping the overlap) reconstructs the original text.
    rebuilt = chunks[0] + "".join(c[3:] for c in chunks[1:])
    assert rebuilt == text


def test_chunk_document_shapes_chunk_dicts():
    loader = ingestion.DocumentLoader(ingestion.TextChunker(chunk_size=10, chunk_overlap=2))
    out = loader.chunk_document("abcdefghijklmnop", metadata={"source": "doc.md"})
    assert len(out) >= 2
    for i, chunk in enumerate(out):
        assert chunk["metadata"]["source"] == "doc.md"
        assert chunk["metadata"]["chunk_index"] == i
        assert chunk["metadata"]["chunk_size"] == len(chunk["content"])


def test_load_from_string_uses_default_metadata():
    loader = ingestion.DocumentLoader()
    doc = loader.load_from_string("hello")
    assert doc["content"] == "hello"
    assert doc["metadata"]["source"] == "string"


def test_load_missing_file_raises(tmp_path):
    loader = ingestion.DocumentLoader()
    with pytest.raises(FileNotFoundError):
        loader.load_text(str(tmp_path / "nope.txt"))
