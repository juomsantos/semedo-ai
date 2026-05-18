"""Configuration module that loads settings from `config.json`.

Behavior:
 Loads defaults from `config.json` located next to this file.

If `config.json` is missing or invalid, sensible hard-coded defaults are used.
A startup warning is printed if the JSON is malformed (so the failure is
visible instead of being silently swallowed) or if it contains keys the
Settings class doesn't know about (a typo would otherwise be ignored and bite
the operator 20 minutes later with confusing downstream errors).
"""

from pathlib import Path
from typing import Optional, Any, Dict
import json
import sys


# Authoritative list of keys the Settings constructor consumes. Anything in
# config.json outside this set is almost certainly a typo and is flagged at
# load time. Keep in sync with the `val(...)` calls below.
_KNOWN_KEYS = frozenset({
    "OLLAMA_HOST",
    "OLLAMA_EMBED_MODEL",
    "RERANK_MODEL",
    "CHROMA_PATH",
    "CHROMA_COLLECTION",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
})


def _warn(msg: str) -> None:
    """Surface a config-loading problem on stderr so it actually gets seen.

    The RAG API is started by the scheduler with stdout/stderr inherited, so
    this lands in the scheduler log alongside the rest of the boot output.
    """
    print(f"[rag_api/config.py] WARNING: {msg}", file=sys.stderr)


def _load_json_config() -> Dict[str, Any]:
    path = Path(__file__).parent / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, json.JSONDecodeError) as e:
        _warn(f"could not load {path.name} ({type(e).__name__}: {e}) — falling back to defaults")
        return {}
    if not isinstance(data, dict):
        _warn(f"{path.name} did not contain a JSON object — falling back to defaults")
        return {}
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        _warn(
            f"{path.name} contains unknown key(s): {', '.join(unknown)} — "
            f"likely a typo. Known keys: {', '.join(sorted(_KNOWN_KEYS))}"
        )
    return data


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
