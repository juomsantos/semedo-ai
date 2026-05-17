"""FastAPI application for RAG API."""

import time
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from config import Settings
from models import (
    IngestRequest, IngestResponse,
    QueryRequest, QueryResponse,
    DocumentListResponse, ErrorResponse,
    HealthResponse,
    MAX_INGEST_CONTENT_CHARS,
)

# Maximum HTTP request body size in bytes — slightly above the ingest content
# cap to leave headroom for JSON framing, document_id, metadata, and UTF-8
# multi-byte expansion (1 char can be up to 4 bytes encoded). Enforced via
# Content-Length so we reject oversized payloads *before* reading the body
# into memory.
_MAX_REQUEST_BODY_BYTES = MAX_INGEST_CONTENT_CHARS * 4 + 64 * 1024
from ingestion import TextChunker, DocumentLoader
from ollama_client import OllamaClient
from vector_store import ChromaDBPersistentClient


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup: load settings and wire components
# ---------------------------------------------------------------------------

settings = Settings()

ollama_client = OllamaClient(
    host=settings.OLLAMA_HOST,
    embed_model=settings.OLLAMA_EMBED_MODEL,
)

# Pass the configured client so vector_store never hits localhost by accident
vector_store = ChromaDBPersistentClient(
    path=settings.CHROMA_PATH,
    collection_name=settings.CHROMA_COLLECTION,
    ollama_client=ollama_client,
)

chunker = TextChunker(
    chunk_size=settings.CHUNK_SIZE,
    chunk_overlap=settings.CHUNK_OVERLAP,
)
document_loader = DocumentLoader(chunker=chunker)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="RAG API",
    description="Retrieval-Augmented Generation API",
    version="1.0.0",
)

# CORS is intentionally restricted to loopback origins.
# The RAG API is consumed by Python agents (server-side, no CORS check) and by
# the Flask dashboard which proxies requests through its own backend — no browser
# ever needs to call this API directly. Allowing arbitrary origins with credentials
# would enable CSRF against /ingest and DELETE /documents/{id}.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_size_limit_middleware(request: Request, call_next):
    """Reject requests whose Content-Length exceeds the ingest cap.

    Stops a malicious or buggy client from streaming a huge body into memory
    before Pydantic validation has a chance to reject it. Returns 413 Payload
    Too Large, which is the correct HTTP status for this condition.

    Note: chunked / unknown-length requests bypass this check — those would
    still hit the Pydantic ``max_length`` ceiling once buffered. Uvicorn's
    own body buffer is bounded by the process memory.
    """
    from starlette.responses import JSONResponse

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "Payload too large",
                        "max_bytes": _MAX_REQUEST_BODY_BYTES,
                        "received_bytes": int(content_length),
                    },
                )
        except ValueError:
            # Malformed Content-Length — let downstream handle it
            pass
    return await call_next(request)


@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{time.time() - start:.3f}s"
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"service": "RAG API", "version": "1.0.0", "docs": "/docs", "health": "/health"}


@app.get("/health", response_model=HealthResponse)
async def health_check():
    healthy = ollama_client.health_check()
    return HealthResponse(
        status="healthy" if healthy else "degraded",
        service="rag-api",
    )


@app.post("/ingest", response_model=IngestResponse)
async def ingest_document(request: IngestRequest):
    """Chunk and embed a document into the vector store."""
    try:
        chunks = document_loader.chunk_document(
            content=request.content,
            metadata=request.metadata or {},
        )

        for chunk in chunks:
            vector_store.add_document(chunk)

        return IngestResponse(
            success=True,
            document_id=request.document_id,
            message=f"Document '{request.document_id}' ingested with {len(chunks)} chunk(s)",
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {e}")
    except Exception as e:
        logger.error(f"Ingestion error: {e}")
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")


@app.post("/query", response_model=QueryResponse)
async def query_documents(request: QueryRequest):
    """Query the vector store; optionally rerank results."""
    try:
        results = vector_store.query(query_text=request.query, top_k=request.top_k)

        if request.use_rerank and results:
            try:
                results = ollama_client.rerank(query=request.query, documents=results)
            except Exception as e:
                logger.warning(f"Reranking failed, returning unranked results: {e}")

        return QueryResponse(
            query=request.query,
            results=results,
            total_results=len(results),
        )
    except Exception as e:
        logger.error(f"Query error: {e}")
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")


@app.get("/documents", response_model=DocumentListResponse)
async def list_documents():
    try:
        docs = vector_store.list_documents()
        return DocumentListResponse(documents=docs, total_count=len(docs))
    except Exception as e:
        logger.error(f"List documents error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list documents: {e}")


@app.get("/documents/{doc_id}")
async def get_document(doc_id: str):
    try:
        doc = vector_store.get_document(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
        return doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get document error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get document: {e}")


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    try:
        if not vector_store.delete_document(doc_id):
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
        return {"message": f"Document '{doc_id}' deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete document error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete document: {e}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
