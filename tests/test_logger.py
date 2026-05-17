"""
Tests for ``scripts/shared/logger.py`` — the file-based AgentLogger used by
every agent script.

We re-point ``PROJECT_ROOT`` at a temp dir so we don't write into the real
``logs/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import shared.logger as logger_mod


@pytest.fixture
def patched_logger_root(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_mod, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _read_log(root: Path, agent_name: str, task_id: str) -> str:
    return (root / "logs" / agent_name / f"{task_id}.log").read_text(encoding="utf-8")


def test_logger_creates_agent_log_directory(patched_logger_root):
    logger_mod.AgentLogger("coder", task_id="t_001")
    assert (patched_logger_root / "logs" / "coder").is_dir()


def test_logger_default_task_id_is_general(patched_logger_root):
    log = logger_mod.AgentLogger("coder")
    assert log.task_id == "general"
    assert log.log_file.name == "general.log"


def test_logger_info_writes_log_line(patched_logger_root, capsys):
    log = logger_mod.AgentLogger("coder", task_id="t_info")
    log.info("hello world")

    contents = _read_log(patched_logger_root, "coder", "t_info")
    assert "INFO" in contents
    assert "hello world" in contents
    assert "[coder]" in contents
    # Also printed to stdout
    captured = capsys.readouterr()
    assert "hello world" in captured.out


def test_logger_warning_uses_WARN_level(patched_logger_root):
    log = logger_mod.AgentLogger("coder", task_id="t_warn")
    log.warning("careful")
    assert "[WARN]" in _read_log(patched_logger_root, "coder", "t_warn")


def test_logger_error_uses_ERROR_level(patched_logger_root):
    log = logger_mod.AgentLogger("coder", task_id="t_err")
    log.error("oops")
    assert "[ERROR]" in _read_log(patched_logger_root, "coder", "t_err")


def test_logger_debug_uses_DEBUG_level(patched_logger_root):
    log = logger_mod.AgentLogger("coder", task_id="t_dbg")
    log.debug("inner state")
    assert "[DEBUG]" in _read_log(patched_logger_root, "coder", "t_dbg")


def test_logger_appends_across_calls(patched_logger_root):
    log = logger_mod.AgentLogger("research", task_id="t_append")
    log.info("first")
    log.info("second")
    contents = _read_log(patched_logger_root, "research", "t_append")
    assert contents.count("INFO") == 2
    assert "first" in contents
    assert "second" in contents


def test_logger_emits_utc_iso_timestamp(patched_logger_root):
    log = logger_mod.AgentLogger("qa", task_id="t_ts")
    log.info("x")
    contents = _read_log(patched_logger_root, "qa", "t_ts")
    # Format: [YYYY-MM-DDTHH:MM:SSZ]
    import re
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", contents)


def test_logger_handles_unicode_message(patched_logger_root):
    log = logger_mod.AgentLogger("coder", task_id="t_unicode")
    log.info("héllo 世界 ✓")
    contents = _read_log(patched_logger_root, "coder", "t_unicode")
    assert "héllo 世界 ✓" in contents
