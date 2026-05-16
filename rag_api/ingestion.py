"""Document ingestion utilities with TextChunker and DocumentLoader."""

import json
import re
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel


class TextChunker:
    """Text chunker with configurable overlap."""

    def __init__(self, chunk_size: int = 1500, chunk_overlap: int = 150):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk(self, text: str) -> List[str]:
        """Split text into chunks with overlap."""
        if not text or len(text) <= self.chunk_size:
            return [text] if text else []

        chunks = []
        start = 0

        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]
            chunks.append(chunk)

            if end >= len(text):
                break

            start = end - self.chunk_overlap

        return chunks

    def chunk_by_sentences(self, text: str, max_sentences: int = 3) -> List[str]:
        """Chunk text by sentences with overlap."""
        if not text:
            return []

        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks = []
        current_chunk = []
        current_length = 0

        for sentence in sentences:
            sentence_len = len(sentence) + 1

            if current_length + sentence_len <= self.chunk_size:
                current_chunk.append(sentence)
                current_length += sentence_len
            else:
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                    overlap_text = ' '.join(current_chunk[-self.chunk_overlap:])
                    current_chunk = [overlap_text] if overlap_text else []
                    current_length = len(overlap_text) + 1 if overlap_text else 0
                current_chunk.append(sentence)
                current_length += sentence_len

        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks


class DocumentLoader:
    """Document loader for various file formats."""

    def __init__(self, chunker: Optional[TextChunker] = None):
        self.chunker = chunker or TextChunker()

    def load_text(self, path: str) -> dict:
        """Load text file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Text file not found: {path}")
        content = path.read_text(encoding='utf-8')
        return {
            'content': content,
            'metadata': {'source': path.name, 'format': 'text', 'path': str(path)}
        }

    def load_markdown(self, path: str) -> dict:
        """Load markdown file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Markdown file not found: {path}")
        content = path.read_text(encoding='utf-8')
        metadata = {'source': path.name, 'format': 'markdown', 'path': str(path)}
        title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
        if title_match:
            metadata['title'] = title_match.group(1).strip()
        return {'content': content, 'metadata': metadata}

    def load_json(self, path: str) -> dict:
        """Load JSON file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"JSON file not found: {path}")
        content = path.read_text(encoding='utf-8')
        data = json.loads(content)
        content_str = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)
        return {
            'content': content_str,
            'metadata': {
                'source': path.name,
                'format': 'json',
                'path': str(path),
                'type': type(data).__name__
            }
        }

    def load(self, path: str) -> dict:
        """Load document based on file extension."""
        path = Path(path)
        ext = path.suffix.lower()
        if ext == '.txt':
            return self.load_text(path)
        elif ext == '.md':
            return self.load_markdown(path)
        elif ext == '.json':
            return self.load_json(path)
        else:
            return self.load_text(path)

    def load_from_string(self, content: str, metadata: Optional[dict] = None) -> dict:
        """Load content from a raw string."""
        return {
            'content': content,
            'metadata': metadata or {'source': 'string', 'format': 'text'}
        }

    def chunk_document(self, content: str, metadata: Optional[dict] = None) -> List[dict]:
        """Chunk document content into a list of chunk dicts."""
        base_meta = metadata or {}
        chunks = self.chunker.chunk(content)
        return [
            {
                'content': chunk,
                'metadata': {**base_meta, 'chunk_index': i, 'chunk_size': len(chunk)}
            }
            for i, chunk in enumerate(chunks)
        ]
