"""Ollama client for embeddings and reranking."""

import math
import requests
from typing import Optional, List, Dict, Any


class OllamaClient:
    """Client for Ollama API interactions."""

    def __init__(
        self,
        host: str = "http://192.168.1.13:11434",
        embed_model: str = "qwen3-embedding:8b",
    ):
        self.host = host.rstrip('/')
        self.base_url = f"{self.host}/api"
        self.embed_model = embed_model

    def health_check(self) -> bool:
        """Check if Ollama server is healthy."""
        try:
            response = requests.get(f"{self.base_url}/tags", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def get_available_models(self) -> List[str]:
        """Get list of available models."""
        try:
            response = requests.get(f"{self.base_url}/tags", timeout=10)
            if response.status_code == 200:
                data = response.json()
                return [model['name'] for model in data.get('models', [])]
        except Exception:
            pass
        return []

    def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate embeddings for text using /api/embeddings.

        Args:
            text: Text to embed
            model: Override the default embed model

        Returns:
            List of embedding floats, or empty list on failure
        """
        model = model or self.embed_model

        try:
            response = requests.post(
                f"{self.base_url}/embeddings",
                json={"model": model, "prompt": text},
                timeout=60,
            )
            if response.status_code == 200:
                return response.json().get('embedding', [])
            raise Exception(f"Embedding failed with HTTP {response.status_code}: {response.text[:200]}")
        except requests.exceptions.Timeout:
            raise Exception("Embedding request timed out")
        except Exception as e:
            raise Exception(f"Embedding error: {str(e)}")

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        """Generate text using Ollama /api/generate."""
        model = model or "qwen3:8b"
        try:
            response = requests.post(
                f"{self.base_url}/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=120,
            )
            if response.status_code == 200:
                return response.json().get('response', '')
            raise Exception(f"Generation failed: HTTP {response.status_code}")
        except Exception as e:
            raise Exception(f"Generation error: {str(e)}")

    def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Rerank documents by cosine similarity of their embeddings vs the query embedding.

        Ollama does not expose a /api/rerank endpoint, so this is implemented
        via the embedding model.  The `model` parameter is accepted for API
        compatibility but the embed_model is used for scoring.

        Args:
            query: Query string
            documents: List of dicts, each with at least a 'content' key
            model: Unused (kept for compatibility)

        Returns:
            Documents sorted by descending cosine similarity score, each with
            an updated 'score' key.
        """
        if not documents:
            return documents

        def cosine_sim(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)

        query_emb = self.embed(query)

        scored = []
        for doc in documents:
            content = doc.get('content', '')
            doc_emb = self.embed(content)
            score = cosine_sim(query_emb, doc_emb) if query_emb and doc_emb else 0.0
            scored.append({**doc, 'score': score})

        return sorted(scored, key=lambda x: x['score'], reverse=True)

    def close(self):
        """Close client resources (no-op for requests-based client)."""
        pass
