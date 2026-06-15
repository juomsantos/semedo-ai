"""
ollama_api_logger.py — Log Ollama API requests and responses for the dashboard chat.

Writes JSON objects to logs/dashboard/ollama_api.jsonl with timestamps.
Each line is: {"timestamp": ISO, "direction": "request|response", "payload": {...}, "session_id": str}
"""

import json
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

# Module-level lock shared across all instances. The dashboard creates one
# OllamaAPILogger in app.py and another in agent_chat.py, both pointing at the
# same file; a shared lock serializes their appends so lines never interleave.
_WRITE_LOCK = threading.Lock()


class OllamaAPILogger:
    """Append-only JSONL logger for Ollama API traffic.

    Writes are done with a short-lived ``open(..., "a")`` per entry rather than
    a persistent ``logging.FileHandler``. Holding a handle open made the file
    undeletable on Windows (multiple instances each kept one open), so
    ``clear_logs()`` silently failed. Open-append-close keeps no lingering
    handle, so the file can always be truncated/removed.
    """

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "ollama_api.jsonl"

    def log_request(self, model: str, messages: list, tools: Optional[list] = None, options: Optional[dict] = None, session_id: Optional[str] = None):
        """Log an outgoing Ollama API request."""
        payload = {
            "model": model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = [self._serialize_tool(t) for t in (tools or [])]
        if options:
            payload["options"] = options

        self._write_entry("request", payload, session_id)

    def log_response(self, response: dict, session_id: Optional[str] = None):
        """Log an incoming Ollama API response."""
        # Serialize the response (it may contain Message objects)
        serialized = self._serialize_response(response)
        self._write_entry("response", serialized, session_id)

    def log_stream_chunk(self, chunk: dict, session_id: Optional[str] = None):
        """Log a streaming chunk from Ollama."""
        serialized = self._serialize_response(chunk)
        self._write_entry("stream_chunk", serialized, session_id)

    def log_error(self, error: str, context: dict, session_id: Optional[str] = None):
        """Log an error from Ollama."""
        payload = {
            "error": error,
            "context": context,
        }
        self._write_entry("error", payload, session_id)

    def _write_entry(self, direction: str, payload: Any, session_id: Optional[str] = None):
        """Append a single log entry as one JSON line (no persistent handle)."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "session_id": session_id or "unknown",
            "payload": payload,
        }
        line = json.dumps(entry, default=str)
        try:
            with _WRITE_LOCK:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            # Logging must never break the chat flow.
            pass

    def _serialize_tool(self, tool: Any) -> Any:
        """Serialize a tool (callable or dict)."""
        if isinstance(tool, dict):
            return tool
        # For callables, just return the name
        if hasattr(tool, '__name__'):
            return {"type": "function", "name": tool.__name__}
        return str(tool)

    def _serialize_response(self, obj: Any) -> Any:
        """Recursively serialize objects to JSON-safe format."""
        if hasattr(obj, '__dict__'):
            # Handle Message objects and other dataclasses
            return {k: self._serialize_response(v) for k, v in obj.__dict__.items()}
        elif isinstance(obj, dict):
            return {k: self._serialize_response(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._serialize_response(item) for item in obj]
        else:
            return obj

    def read_logs(self, limit: int = 100, session_id: Optional[str] = None) -> list:
        """Read recent log entries, optionally filtered by session."""
        if not self.log_file.exists():
            return []

        entries = []
        try:
            with open(self.log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if session_id is None or entry.get("session_id") == session_id:
                            entries.append(entry)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

        # Return most recent entries up to limit
        return entries[-limit:] if limit else entries

    def clear_logs(self):
        """Remove all log entries. Returns True on success, False otherwise."""
        with _WRITE_LOCK:
            try:
                if self.log_file.exists():
                    self.log_file.unlink()
                return True
            except Exception:
                # Fallback: if the file can't be removed, truncate it in place.
                try:
                    open(self.log_file, "w", encoding="utf-8").close()
                    return True
                except Exception:
                    return False
