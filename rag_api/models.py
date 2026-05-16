"""Pydantic v2 request/response models for the RAG API."""

from typing import Optional, List
from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    """Request model for document ingestion."""
    document_id: str = Field(..., description="Unique identifier for the document")
    content: str = Field(..., description="Document content to ingest")
    metadata: Optional[dict] = Field(default_factory=dict, description="Document metadata")


class IngestResponse(BaseModel):
    """Response model for document ingestion."""
    success: bool = Field(..., description="Whether ingestion was successful")
    document_id: Optional[str] = Field(None, description="Ingested document ID")
    message: str = Field(..., description="Response message")


class QueryRequest(BaseModel):
    """Request model for querying the vector store."""
    query: str = Field(..., description="Query text to search for")
    top_k: int = Field(default=5, ge=1, le=100, description="Number of results to return")
    use_rerank: bool = Field(default=False, description="Whether to use reranking")


class QueryResult(BaseModel):
    """Model for a single query result."""
    document_id: str = Field(..., description="Document ID")
    content: str = Field(..., description="Document content")
    score: float = Field(..., description="Relevance score")
    metadata: Optional[dict] = Field(default=None, description="Document metadata")


class QueryResponse(BaseModel):
    """Response model for query results."""
    query: str = Field(..., description="Original query")
    results: List[QueryResult] = Field(..., description="List of query results")
    total_results: int = Field(..., description="Total number of results")


class DocumentListResponse(BaseModel):
    """Response model for listing documents."""
    documents: List[dict] = Field(..., description="List of document metadata")
    total_count: int = Field(..., description="Total number of documents")


class ErrorResponse(BaseModel):
    """Response model for errors."""
    error: str = Field(..., description="Error message")
    status_code: int = Field(..., description="HTTP status code")


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str = Field(..., description="Health status")
    service: str = Field(default="rag-api", description="Service name")
