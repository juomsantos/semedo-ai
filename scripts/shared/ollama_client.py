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
        self.last_token_counts = {"prompt": 0, "completion": 0}

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

        # Capture token counts
        self.last_token_counts = {
            "prompt": data.get("prompt_eval_count", 0),
            "completion": data.get("eval_count", 0),
        }

        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as e:
            raise OllamaError(f"Unexpected Ollama response shape: {data}") from e

    def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
    ) -> dict:
        """
        Send a chat request with tool definitions using Ollama's tool-calling API.

        Args:
            model:       Ollama model name.
            messages:    Full message history in OpenAI format:
                           [{"role": "system"|"user"|"assistant"|"tool", "content": "..."}]
                         For tool result messages use:
                           {"role": "tool", "content": "<result string>"}
            tools:       List of tool definitions in OpenAI function format:
                           [{"type": "function", "function": {
                               "name": "...", "description": "...",
                               "parameters": {"type": "object", "properties": {...}, "required": [...]}
                           }}]
            temperature: Sampling temperature (default 0.3).

        Returns:
            A dict with one of two shapes:
              - Final answer:  {"type": "text", "content": "<string>"}
              - Tool call:     {"type": "tool_call", "name": "<tool_name>", "arguments": {<dict>}}

        Raises:
            OllamaError on network or API failure.
        """
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
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

        # Capture token counts
        self.last_token_counts = {
            "prompt": data.get("prompt_eval_count", 0),
            "completion": data.get("eval_count", 0),
        }

        try:
            message = data["message"]
        except (KeyError, TypeError) as e:
            raise OllamaError(f"Unexpected Ollama response shape: {data}") from e

        # Model wants to call a tool
        tool_calls = message.get("tool_calls")
        if tool_calls:
            call = tool_calls[0]  # handle one call per turn
            fn = call.get("function", {})
            name = fn.get("name", "")
            arguments = fn.get("arguments", {})
            # arguments may arrive as a JSON string in some Ollama versions
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            return {"type": "tool_call", "name": name, "arguments": arguments}

        # Model returned a final text answer
        content = message.get("content", "")
        return {"type": "text", "content": content}

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
