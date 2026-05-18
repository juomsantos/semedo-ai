"""
Pytest configuration and shared fixtures.

The agents use a module-level ``PROJECT_ROOT`` (computed from ``__file__``) to
locate the filesystem-based task queue. To test the queue helpers in isolation
without touching the real ``inbox/``, ``processing/``, etc., the ``fake_project``
fixture builds a fully-populated temp project tree and monkey-patches
``PROJECT_ROOT`` in every module that captured it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable as `shared.*` and `agent_*`
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# Folders the agents expect to find under PROJECT_ROOT.
_PIPELINE_FOLDERS = (
    "inbox",
    "processing",
    "validation",
    "outbox",
    "failed",
    "context",
    "logs",
    "agents/orchestrator",
    "agents/coder/inbox",
    "agents/research/inbox",
    "agents/qa/inbox",
    "agents/claude-code/inbox",
    "agents/claude-code/pending",
)


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """
    Build a complete temp project tree and re-point ``PROJECT_ROOT`` to it.

    Returns the project root ``Path``. The original ``PROJECT_ROOT`` value in
    every importer module is restored on teardown by ``monkeypatch``.
    """
    for folder in _PIPELINE_FOLDERS:
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    # Patch PROJECT_ROOT in every module that imported it. Each `from X import
    # PROJECT_ROOT` creates a separate binding — patching only `shared.task_io`
    # would leave the orchestrator's copy stale.
    import shared.task_io as task_io_mod
    monkeypatch.setattr(task_io_mod, "PROJECT_ROOT", tmp_path)

    # token_logger, logger, and any other shared modules with their own PROJECT_ROOT
    try:
        import shared.token_logger as token_logger_mod
        monkeypatch.setattr(token_logger_mod, "PROJECT_ROOT", tmp_path)
    except ImportError:
        pass

    try:
        import shared.logger as logger_mod
        monkeypatch.setattr(logger_mod, "PROJECT_ROOT", tmp_path)
    except ImportError:
        pass

    # agent_boilerplate captures PROJECT_ROOT via `from shared.task_io import
    # PROJECT_ROOT`, so it needs its own patch (same reason token_logger does).
    try:
        import shared.agent_boilerplate as agent_boilerplate_mod
        monkeypatch.setattr(agent_boilerplate_mod, "PROJECT_ROOT", tmp_path)
    except ImportError:
        pass

    return tmp_path


@pytest.fixture
def sample_task_meta():
    """Return a minimal valid task frontmatter dict."""
    return {
        "id": "task_20260101_120000_000001",
        "type": "code",
        "priority": "medium",
        "created_by": "claude-cowork",
        "created_at": "2026-01-01T12:00:00",
        "assigned_to": "orchestrator",
        "status": "pending",
        "output_path": "outbox/task_20260101_120000_000001_result.md",
        "context_files": [],
    }
