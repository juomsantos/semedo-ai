"""ChromaDB persistent client wrapper for vector storage."""

import chromadb
from typing import Optional, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ollama_client import OllamaClient


class ChromaDBPersistentClient:
    """ChromaDB persistent client wrapper.

    Accepts an injected OllamaClient so embeddings always use the configured
    Ollama server rather than defaulting to localhost.
    """

    def __init__(
        self,
        path: str = "./chroma_db",
        collection_name: str = "documents",
        ollama_client: Optional["OllamaClient"] = None,
    ):
        self.path = path
        self.collection_name = collection_name
        self._ollama_client = ollama_client
        self._client: Optional[chromadb.PersistentClient] = None
        self._collection = None

    def _ensure_client(self) -> chromadb.PersistentClient:
        if self._client is None:
            self._client = chromadb.PersistentClient(path=self.path)
        return self._client

    def _ensure_collection(self):
        if self._collection is None:
            self._collection = self._ensure_client().get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _embed_content(self, content: str) -> List[float]:
        """Embed content using the injected OllamaClient.

        Falls back to a zero vector if embedding fails, so ingestion/query
        never hard-crashes (though retrieval quality will be zero for that chunk).
        """
        try:
            if self._ollama_client is not None:
                embedding = self._ollama_client.embed(content)
            else:
                # Fallback: import and create a client with default settings
                from ollama_client import OllamaClient
                embedding = OllamaClient().embed(content)
            return embedding if embedding else [0.0] * 4096
        except Exception:
            return [0.0] * 4096

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_documents(self, documents: List[Dict[str, Any]]) -> List[str]:
        """Add a batch of documents to the vector store."""
        if not documents:
            return []

        collection = self._ensure_collection()
        ids = []

        for doc in documents:
            content = doc.get('content', '')
            metadata = doc.get('metadata', {})
            doc_id = doc.get('id') or f"doc_{abs(hash(content)) % 10_000_000}"

            embedding = self._embed_content(content)
            collection.add(
                documents=[content],
                ids=[doc_id],
                embeddings=[embedding],
                metadatas=[metadata],
            )
            ids.append(doc_id)

        return ids

    def add_document(self, document: Dict[str, Any]) -> str:
        """Add a single document; returns its ID."""
        ids = self.add_documents([document])
        return ids[0] if ids else ""

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def query(self, query_text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Query the vector store and return the top-k results."""
        collection = self._ensure_collection()
        embedding = self._embed_content(query_text)

        results = collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
        )

        # collection.query() returns nested lists (one row per query vector)
        query_results = []
        ids = results.get('ids', [[]])[0]
        docs = results.get('documents', [[]])[0]
        dists = results.get('distances', [[]])[0]
        metas = results.get('metadatas', [[]])[0]

        for i, doc_id in enumerate(ids):
            if doc_id:
                query_results.append({
                    'document_id': doc_id,
                    'content': docs[i] if i < len(docs) else '',
                    'score': dists[i] if i < len(dists) else 0.0,
                    'metadata': metas[i] if i < len(metas) else {},
                })

        return query_results

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a document by its ID."""
        collection = self._ensure_collection()
        # collection.get() returns flat lists (not nested)
        results = collection.get(ids=[doc_id])

        ids = results.get('ids', [])
        if not ids:
            return None

        docs = results.get('documents') or ['']
        metas = results.get('metadatas') or [{}]
        return {
            'document_id': ids[0],
            'content': docs[0] if docs else '',
            'metadata': metas[0] if metas else {},
        }

    def list_documents(self) -> List[Dict[str, Any]]:
        """List metadata for all documents in the collection."""
        collection = self._ensure_collection()
        # collection.get() with no ids returns flat lists
        results = collection.get(include=['metadatas'])

        ids = results.get('ids', [])
        metas = results.get('metadatas') or []

        return [
            {'id': doc_id, 'metadata': metas[i] if i < len(metas) else {}}
            for i, doc_id in enumerate(ids)
            if doc_id
        ]

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document by ID. Returns True if deleted."""
        collection = self._ensure_collection()
        try:
            collection.delete(ids=[doc_id])
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def count_documents(self) -> int:
        """Return the number of documents in the collection."""
        try:
            return self._ensure_collection().count()
        except Exception:
            return 0

    def clear(self):
        """Delete all documents from the collection."""
        self._ensure_collection().delete()

    def close(self):
        """Release client references."""
        self._client = None
        self._collection = None
