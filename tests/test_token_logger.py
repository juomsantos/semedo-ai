"""
Tests for ``scripts/shared/token_logger.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import shared.token_logger as token_logger


def test_log_tokens_writes_jsonl_for_real_task_id(fake_project):
    token_logger.log_tokens(
        agent_name="coder",
        task_id="task_20260101_120000_000001",
        prompt_tokens=42,
        completion_tokens=17,
    )

    log_file = fake_project / "logs" / "coder" / "tokens.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["task_id"] == "task_20260101_120000_000001"
    assert entry["prompt"] == 42
    assert entry["completion"] == 17
    assert "ts" in entry


def test_log_tokens_appends_multiple_calls(fake_project):
    for i in range(3):
        token_logger.log_tokens(
            agent_name="research",
            task_id=f"task_20260101_120000_{i:06d}",
            prompt_tokens=i * 10,
            completion_tokens=i * 5,
        )
    log_file = fake_project / "logs" / "research" / "tokens.jsonl"
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_log_tokens_skips_invalid_task_id(fake_project):
    """IDs that don't match the production format are dropped silently."""
    token_logger.log_tokens(
        agent_name="coder",
        task_id="test_id_not_real",
        prompt_tokens=1,
        completion_tokens=1,
    )
    log_file = fake_project / "logs" / "coder" / "tokens.jsonl"
    assert not log_file.exists()


def test_log_tokens_creates_agent_log_directory(fake_project):
    """Per-agent directory is created on first call."""
    custom_log_dir = fake_project / "logs" / "newagent"
    assert not custom_log_dir.exists()

    token_logger.log_tokens(
        agent_name="newagent",
        task_id="task_20260101_120000_000099",
        prompt_tokens=1,
        completion_tokens=1,
    )
    assert custom_log_dir.is_dir()
    assert (custom_log_dir / "tokens.jsonl").exists()


def test_log_tokens_writes_valid_iso_timestamp(fake_project):
    token_logger.log_tokens(
        agent_name="qa",
        task_id="task_20260101_120000_000001",
        prompt_tokens=1,
        completion_tokens=1,
    )
    line = (fake_project / "logs" / "qa" / "tokens.jsonl").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    # Format: YYYY-MM-DDTHH:MM:SSZ
    assert entry["ts"].endswith("Z")
    assert len(entry["ts"]) == 20
