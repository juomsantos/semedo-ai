"""
Tests for ``scripts/shared/logger.py`` — the file-based AgentLogger used by
every agent script.

Leverages the ``fake_project`` fixture from conftest.py to set up a temp
project tree with PROJECT_ROOT patched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import shared.logger as logger_mod


def _read_log(root: Path, agent_name: str, task_id: str) -> str:
    return (root / "logs" / agent_name / f"{task_id}.log").read_text(encoding="utf-8")


def test_logger_creates_agent_log_directory(fake_project):
    logger_mod.AgentLogger("coder", task_id="t_001")
    assert (fake_project / "logs" / "coder").is_dir()


def test_logger_default_task_id_is_general(fake_project):
    log = logger_mod.AgentLogger("coder")
    assert log.task_id == "general"
    assert log.log_file.name == "general.log"


def test_logger_info_writes_log_line(fake_project, capsys):
    log = logger_mod.AgentLogger("coder", task_id="t_info")
    log.info("hello world")

    contents = _read_log(fake_project, "coder", "t_info")
    assert "INFO" in contents
    assert "hello world" in contents
    assert "[coder]" in contents
    # Also printed to stdout
    captured = capsys.readouterr()
    assert "hello world" in captured.out


def test_logger_warning_uses_WARN_level(fake_project):
    log = logger_mod.AgentLogger("coder", task_id="t_warn")
    log.warning("careful")
    assert "[WARN]" in _read_log(fake_project, "coder", "t_warn")


def test_logger_error_uses_ERROR_level(fake_project):
    log = logger_mod.AgentLogger("coder", task_id="t_err")
    log.error("oops")
    assert "[ERROR]" in _read_log(fake_project, "coder", "t_err")


def test_logger_debug_uses_DEBUG_level(fake_project):
    log = logger_mod.AgentLogger("coder", task_id="t_dbg")
    log.debug("inner state")
    assert "[DEBUG]" in _read_log(fake_project, "coder", "t_dbg")


def test_logger_appends_across_calls(fake_project):
    log = logger_mod.AgentLogger("research", task_id="t_append")
    log.info("first")
    log.info("second")
    contents = _read_log(fake_project, "research", "t_append")
    assert contents.count("INFO") == 2
    assert "first" in contents
    assert "second" in contents


def test_logger_emits_utc_iso_timestamp(fake_project):
    log = logger_mod.AgentLogger("qa", task_id="t_ts")
    log.info("x")
    contents = _read_log(fake_project, "qa", "t_ts")
    # Format: [YYYY-MM-DDTHH:MM:SSZ]
    import re
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", contents)


def test_logger_handles_unicode_message(fake_project):
    log = logger_mod.AgentLogger("coder", task_id="t_unicode")
    log.info("héllo 世界 ✓")
    contents = _read_log(fake_project, "coder", "t_unicode")
    assert "héllo 世界 ✓" in contents
