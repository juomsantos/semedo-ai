"""
Tests for ``scripts/shared/config.py`` — the ``ProjectConfig`` accessors and
JSON loader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import shared.config as cfg


# ---------------------------------------------------------------------------
# ProjectConfig accessors
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> cfg.ProjectConfig:
    base = {
        "ollama": {"base_url": "http://example:11434", "timeout": 240},
        "agents": {
            "coder": {"model": "qwen3.5:9b", "process_timeout": 600},
            "claude-code": {"cli": True, "timeout": 300, "process_timeout": 600},
        },
        "web_search": {"ollama_api_key": "abc123"},
        "scheduler": {"enable_timer_polling": False},
        "rag_api": {"url": "http://example:8000"},
    }
    base.update(overrides)
    return cfg.ProjectConfig(base)


def test_ollama_base_url_returns_configured():
    assert _make_config().ollama_base_url() == "http://example:11434"


def test_ollama_base_url_returns_default_when_missing():
    c = cfg.ProjectConfig({})
    assert c.ollama_base_url() == "http://localhost:11434"


def test_ollama_timeout_returns_configured():
    assert _make_config().ollama_timeout() == 240


def test_ollama_timeout_returns_default_when_missing():
    assert cfg.ProjectConfig({}).ollama_timeout() == 120


def test_agent_model_returns_configured():
    assert _make_config().agent_model("coder") == "qwen3.5:9b"


def test_agent_model_returns_none_when_agent_uses_cli():
    """claude-code has `cli: true` and no `model` — accessor must return None."""
    assert _make_config().agent_model("claude-code") is None


def test_agent_model_returns_none_for_unknown_agent():
    assert _make_config().agent_model("nope") is None


def test_agent_uses_cli_true_for_claude_code():
    assert _make_config().agent_uses_cli("claude-code") is True


def test_agent_uses_cli_false_for_ollama_agent():
    assert _make_config().agent_uses_cli("coder") is False


def test_agent_uses_cli_false_for_unknown_agent():
    assert _make_config().agent_uses_cli("nope") is False


def test_agent_timeout_returns_per_agent_value():
    assert _make_config().agent_timeout("claude-code") == 300


def test_agent_timeout_falls_back_to_ollama_timeout():
    # `coder` has no `timeout` key — falls back to ollama_timeout
    assert _make_config().agent_timeout("coder") == 240


def test_agent_process_timeout_returns_configured():
    assert _make_config().agent_process_timeout("coder") == 600


def test_agent_process_timeout_default_300():
    c = cfg.ProjectConfig({"agents": {"x": {}}})
    assert c.agent_process_timeout("x") == 300


def test_agent_options_returns_configured_dict():
    c = cfg.ProjectConfig({
        "agents": {
            "coder": {
                "options": {"temperature": 0.2, "top_p": 0.9, "min_p": 0.05, "seed": 42},
            }
        }
    })
    opts = c.agent_options("coder")
    assert opts == {"temperature": 0.2, "top_p": 0.9, "min_p": 0.05, "seed": 42}


def test_agent_options_returns_empty_when_missing():
    # `coder` in _make_config has no options block
    assert _make_config().agent_options("coder") == {}


def test_agent_options_returns_empty_for_unknown_agent():
    assert _make_config().agent_options("nope") == {}


def test_agent_options_returns_empty_when_null():
    c = cfg.ProjectConfig({"agents": {"x": {"options": None}}})
    assert c.agent_options("x") == {}


def test_agent_options_returns_independent_copy():
    """Mutating the returned dict must not affect the underlying config."""
    c = cfg.ProjectConfig({"agents": {"x": {"options": {"temperature": 0.1}}}})
    opts = c.agent_options("x")
    opts["temperature"] = 9.9
    assert c.agent_options("x")["temperature"] == 0.1


def test_agent_thinking_returns_configured():
    c = cfg.ProjectConfig({"agents": {"x": {"thinking": True}, "y": {"thinking": False}}})
    assert c.agent_thinking("x") is True
    assert c.agent_thinking("y") is False


def test_agent_thinking_returns_none_when_missing():
    assert _make_config().agent_thinking("coder") is None


def test_agent_thinking_returns_none_for_unknown_agent():
    assert _make_config().agent_thinking("nope") is None


def test_web_search_api_key_returns_configured():
    assert _make_config().web_search_api_key() == "abc123"


def test_web_search_api_key_returns_empty_when_missing():
    assert cfg.ProjectConfig({}).web_search_api_key() == ""


def test_scheduler_enable_timer_polling_returns_configured():
    assert _make_config().scheduler_enable_timer_polling() is False


def test_scheduler_enable_timer_polling_default_true():
    assert cfg.ProjectConfig({}).scheduler_enable_timer_polling() is True


def test_rag_api_url_returns_configured():
    assert _make_config().rag_api_url() == "http://example:8000"


def test_rag_api_url_default_localhost():
    assert cfg.ProjectConfig({}).rag_api_url() == "http://localhost:8000"


def test_list_agents_returns_keys():
    assert set(_make_config().list_agents()) == {"coder", "claude-code"}


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_reads_json_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ollama": {"base_url": "http://t:1"}}), encoding="utf-8")

    loaded = cfg.load_config(path)
    assert loaded.ollama_base_url() == "http://t:1"


def test_load_config_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        cfg.load_config(tmp_path / "missing.json")


def test_load_config_raises_on_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        cfg.load_config(path)


def test_load_config_default_path_resolves_to_project_root():
    """When no path is supplied, load_config looks for config.json in the repo root."""
    # The real config.json exists; just confirm it loads without error.
    loaded = cfg.load_config()
    assert isinstance(loaded, cfg.ProjectConfig)
