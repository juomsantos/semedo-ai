"""
ollama_client.py — Thin wrapper around the Ollama Python library.

Usage:
    from shared.ollama_client import OllamaClient

    client = OllamaClient()
    response = client.chat(
        model="qwen3:9b",
        system_prompt="You are a helpful assistant.",
        user_message="Summarize this text: ...",
    )
    print(response)  # plain string

    # Native tool calling — pass actual Python functions as tools.
    # The ollama library introspects their type annotations and docstrings
    # to auto-generate JSON schemas; no manual WEB_SEARCH_TOOL dict needed.
    from shared.web_search import web_search, web_fetch
    result = client.chat_with_tools(
        model="qwen3:9b",
        messages=[{"role": "user", "content": "What is the latest Python version?"}],
        tools=[web_search, web_fetch],
    )

Ollama configuration (base_url, timeout) is loaded from config.json.
"""

import json
from typing import Optional, Callable, Union
from pathlib import Path

import ollama as _ollama

# Try to load from config, fall back to defaults
try:
    from shared.config import load_config
    _config = load_config()
    OLLAMA_BASE_URL = _config.ollama_base_url()
    DEFAULT_TIMEOUT = _config.ollama_timeout()
except Exception:
    OLLAMA_BASE_URL = "http://192.168.1.13:11434"
    DEFAULT_TIMEOUT = 300


class OllamaError(Exception):
    pass


# A tool can be a Python callable (native) or an OpenAI-format JSON dict
Tool = Union[Callable, dict]


class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.last_token_counts = {"prompt": 0, "completion": 0}
        self._client = _ollama.Client(host=self.base_url)

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

        try:
            response = self._client.chat(
                model=model,
                messages=messages,
                options={"temperature": temperature},
            )
        except _ollama.ResponseError as e:
            raise OllamaError(f"Ollama API error: {e}") from e
        except Exception as e:
            _msg = str(e).lower()
            if "connect" in _msg or "connection" in _msg:
                raise OllamaError(
                    f"Cannot connect to Ollama at {self.base_url}. Is it running?"
                )
            if "timeout" in _msg or "timed out" in _msg:
                raise OllamaError(f"Ollama request timed out after {self.timeout}s.")
            raise OllamaError(f"Ollama error: {e}") from e

        self.last_token_counts = {
            "prompt": response.prompt_eval_count or 0,
            "completion": response.eval_count or 0,
        }
        return response.message.content or ""

    def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[Tool],
        temperature: float = 0.3,
    ) -> dict:
        """
        Send a chat request with tool definitions using Ollama's tool-calling API.

        Tools can be:
          - Python callables (native): the ollama library reads their type
            annotations and docstrings to generate JSON schemas automatically.
          - OpenAI-format dicts: {"type": "function", "function": {...}}

        Args:
            model:       Ollama model name.
            messages:    Full message history in OpenAI format.
                         Tool result messages should be:
                           {"role": "tool", "content": "<result>", "tool_name": "<name>"}
            tools:       List of Python callables or OpenAI tool-definition dicts.
            temperature: Sampling temperature (default 0.3).

        Returns:
            A dict with one of two shapes:
              - Final answer:  {"type": "text", "content": "<string>"}
              - Tool call:     {"type": "tool_call", "name": "<tool_name>",
                                "arguments": {<dict>}, "raw_message": <Message>}

        Raises:
            OllamaError on network or API failure.
        """
        try:
            response = self._client.chat(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                options={"temperature": temperature},
            )
        except _ollama.ResponseError as e:
            raise OllamaError(f"Ollama API error: {e}") from e
        except Exception as e:
            _msg = str(e).lower()
            if "connect" in _msg or "connection" in _msg:
                raise OllamaError(
                    f"Cannot connect to Ollama at {self.base_url}. Is it running?"
                )
            if "timeout" in _msg or "timed out" in _msg:
                raise OllamaError(f"Ollama request timed out after {self.timeout}s.")
            raise OllamaError(f"Ollama error: {e}") from e

        self.last_token_counts = {
            "prompt": response.prompt_eval_count or 0,
            "completion": response.eval_count or 0,
        }

        message = response.message

        # Model wants to call a tool
        if message.tool_calls:
            call = message.tool_calls[0]  # handle one call per turn
            name = call.function.name
            arguments = call.function.arguments
            # arguments may arrive as a JSON string in some Ollama versions
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            return {
                "type": "tool_call",
                "name": name,
                "arguments": arguments,
                "raw_message": message,  # lets callers append the message directly
            }

        # Model returned a final text answer
        return {"type": "text", "content": message.content or ""}

    def is_available(self) -> bool:
        """Return True if Ollama is reachable."""
        try:
            self._client.list()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return list of available local model names."""
        try:
            response = self._client.list()
            return [m.model for m in response.models]
        except Exception as e:
            raise OllamaError(f"Could not list models: {e}")
