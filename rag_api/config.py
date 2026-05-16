"""Configuration module using environment variables with sensible defaults."""

import os
from typing import Optional


class Settings:
    """Application settings loaded from environment variables."""

    def __init__(self):
        self.OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://192.168.1.13:11434")
        self.OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3-embedding:8b")
        self.OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:8b")
        self.RERANK_MODEL: Optional[str] = os.getenv("RERANK_MODEL", "MedAIBase/Qwen3-VL-Reranker:2b")

        self.CHROMA_PATH: str = os.getenv("CHROMA_PATH", "./chroma_db")
        self.CHROMA_COLLECTION: str = os.getenv("CHROMA_COLLECTION", "documents")

        self.CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "512"))
        self.CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))


