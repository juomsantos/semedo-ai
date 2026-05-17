"""
chat_session.py — In-memory session store for chat history.
"""

import uuid
from typing import List, Dict, Optional


class ChatSessionStore:
    """Thread-safe in-memory store for chat sessions."""

    def __init__(self, max_history_turns: int = 20):
        self.max_history_turns = max_history_turns
        self.sessions: Dict[str, List[Dict]] = {}

    def new_session(self) -> str:
        """Create a new session and return its UUID."""
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = []
        return session_id

    def get_history(self, session_id: str) -> List[Dict]:
        """Get conversation history for a session. Auto-creates if missing."""
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        return self.sessions[session_id]

    def append(self, session_id: str, role: str, content: str) -> None:
        """Append a message to the session history. Enforces max_history_turns."""
        if session_id not in self.sessions:
            self.sessions[session_id] = []

        self.sessions[session_id].append({"role": role, "content": content})

        # Trim to max_history_turns pairs (user+assistant = 1 pair)
        # Keep only the last N user+assistant pairs
        history = self.sessions[session_id]
        if len(history) > self.max_history_turns * 2:
            # Drop oldest pair
            self.sessions[session_id] = history[2:]

    def clear(self, session_id: str) -> None:
        """Clear all history for a session."""
        if session_id in self.sessions:
            self.sessions[session_id] = []
