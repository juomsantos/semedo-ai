"""
ollama_client.py — Thin wrapper around the Ollama REST API.

Usage:
    from shared.ollama_client import OllamaClient

    client = OllamaClient()
    response = client.chat(
        model="qwen3:9b",
        system_prompt="You are a helpful assistant.",
        user_message="Summarize this text: ...",
    )
    print(response)  # plain string

Ollama configuration (base_url, timeout) is loaded from config.json.
"""

import requests
import json
from typing import Optional
from pathlib import Path

# Try to load from config, fall back to defaults
try:
    from shared.config import load_config
    _config = load_config()
    OLLAMA_BASE_URL = _config.ollama_base_url()
    DEFAULT_TIMEOUT = _config.ollama_timeout()
except Exception:
    # Fallback defaults if config loading fails
    OLLAMA_BASE_URL = "http://192.168.1.13:11434"
    DEFAULT_TIMEOUT = 120  # seconds — local models can be slow on large prompts


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(
        self,
        model: str,
        user_message: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        """
        Send a chat completion request to Ollama.
        Returns the assistant's reply as a plain string.
        Raises OllamaError on failure.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }

        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise OllamaError(
                f"Cannot connect to Ollama at {self.base_url}. Is it running?"
            )
        except requests.exceptions.Timeout:
            raise OllamaError(f"Ollama request timed out after {self.timeout}s.")
        except requests.exceptions.HTTPError as e:
            raise OllamaError(f"Ollama HTTP error: {e} — {resp.text}")

        data = resp.json()

        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as e:
            raise OllamaError(f"Unexpected Ollama response shape: {data}") from e

    def is_available(self) -> bool:
        """Return True if Ollama is reachable."""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def list_models(self) -> list[str]:
        """Return list of available local model names."""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception as e:
            raise OllamaError(f"Could not list models: {e}")
