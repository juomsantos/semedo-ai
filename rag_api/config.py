"""Configuration module that loads settings from `config.json`.

Behavior:
 Loads defaults from `config.json` located next to this file.

If `config.json` is missing or invalid, sensible hard-coded defaults are used.
"""

from pathlib import Path
from typing import Optional, Any, Dict
import json


def _load_json_config() -> Dict[str, Any]:
    path = Path(__file__).parent / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
            if not isinstance(data, dict):
                return {}
            return data
    except Exception:
        return {}


class Settings:
    """Application settings loaded from `config.json` only."""

    def __init__(self):
        cfg = _load_json_config()

        def val(key: str, default: Any = None):
            return cfg.get(key, default)

        self.OLLAMA_HOST: str = val("OLLAMA_HOST", "http://192.168.1.13:11434")
        self.OLLAMA_EMBED_MODEL: str = val("OLLAMA_EMBED_MODEL", "qwen3-embedding:8b")
        self.RERANK_MODEL: Optional[str] = val("RERANK_MODEL", "MedAIBase/Qwen3-VL-Reranker:2b")

        self.CHROMA_PATH: str = val("CHROMA_PATH", "./chroma_db")
        self.CHROMA_COLLECTION: str = val("CHROMA_COLLECTION", "documents")

        # Ensure numeric values are ints even if read from JSON as strings
        try:
            self.CHUNK_SIZE: int = int(val("CHUNK_SIZE", 1500))
        except (TypeError, ValueError):
            self.CHUNK_SIZE = 1500

        try:
            self.CHUNK_OVERLAP: int = int(val("CHUNK_OVERLAP", 150))
        except (TypeError, ValueError):
            self.CHUNK_OVERLAP = 150

