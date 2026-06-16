"""
Tests for orchestration/recovery.py — the four startup recovery passes.

These run at every orchestrator startup and are reliability-critical: a
regression here silently strands tasks in the wrong folder. They are pure
file-IO against the queue, so they test well against the ``fake_project`` tree.

Fixture note: `recovery.py` reads `INBOX` / `WORKER_INBOXES` via
`from agent_orchestrator import ...`. Those constants are computed at
agent_orchestrator import time from the *real* PROJECT_ROOT, so the
`recovery_env` fixture re-points them (and PROJECT_ROOT) at the temp tree —
otherwise the functions would move files into the real repo.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import frontmatter
import pytest

import shared.task_io as task_io


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _Log:
    """Minimal AgentLogger stand-in that records messages by level."""

    def __init__(self):
        self.records = []

    def _add(self, level, msg):
        self.records.append((level, str(msg)))

    def info(self, msg):
        self._add("info", msg)

    def warning(self, msg):
        self._add("warning", msg)

    def error(self, msg):
        self._add("error", msg)

    def debug(self, msg):
        self._add("debug", msg)

    def text(self):
        return "\n".join(m for _, m in self.records)


@pytest.fixture
def recovery_env(fake_project, monkeypatch):
    """Import recovery + re-point agent_orchestrator's INBOX/WORKER_INBOXES."""
    import agent_orchestrator as ao

    monkeypatch.setattr(ao, "PROJECT_ROOT", fake_project)
    monkeypatch.setattr(ao, "INBOX", fake_project / "inbox")
    monkeypatch.setattr(ao, "WORKER_INBOXES", {
        "coder": fake_project / "agents" / "coder" / "inbox",
        "research": fake_project / "agents" / "research" / "inbox",
        "qa": fake_project / "agents" / "qa" / "inbox",
        "claude-code": fake_project / "agents" / "claude-code" / "inbox",
        "pending_approval": fake_project / "agents" / "claude-code" / "pending",
    })

    import orchestration.recovery as recovery
    return recovery


def _write_task(
    folder: Path,
    task_id: str,
    *,
    status: str = "pending",
    assigned_to: str = "coder",
    task_type: str = "code",
    parent_task_id: str | None = None,
    output_path: str | None = None,
    stall_retry_count: int | None = None,
    created_at: str = "2026-01-01T10:00:00",
    body: str = "## Task\n\nDo the thing.\n",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": task_id,
        "type": task_type,
        "priority": "medium",
        "created_by": "test",
        "created_at": created_at,
        "assigned_to": assigned_to,
        "status": status,
    }
    if output_path is not None:
        meta["output_path"] = output_path
    if parent_task_id is not None:
        meta["parent_task_id"] = parent_task_id
    if stall_retry_count is not None:
        meta["stall_retry_count"] = stall_retry_count
    post = frontmatter.Post(body, **meta)
    path = folder / f"{task_id}.task.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _age(path: Path, seconds: int):
    """Backdate a file's mtime by `seconds`."""
    old = time.time() - seconds
    os.utime(path, (old, old))


# ===========================================================================
# recover_orphaned_tasks — pending parents in processing/ → inbox/
# ===========================================================================


def test_orphaned_pending_parent_returned_to_inbox(recovery_env, fake_project):
    proc = fake_project / "processing"
    _write_task(proc, "task_p1", status="pending", assigned_to="orchestrator")

    recovery_env.recover_orphaned_tasks(_Log())

    assert not (proc / "task_p1.task.md").exists()
    assert (fake_project / "inbox" / "task_p1.task.md").exists()


def test_dispatched_parent_left_in_processing(recovery_env, fake_project):
    proc = fake_project / "processing"
    _write_task(proc, "task_p2", status="dispatched", assigned_to="orchestrator")

    recovery_env.recover_orphaned_tasks(_Log())

    assert (proc / "task_p2.task.md").exists()
    assert not (fake_project / "inbox" / "task_p2.task.md").exists()


# ===========================================================================
# recover_processing_subtasks — stale status:processing subtasks → worker inbox
# ===========================================================================


def test_stale_processing_subtask_returned_to_worker(recovery_env, fake_project):
    proc = fake_project / "processing"
    p = _write_task(proc, "task_s1", status="processing", assigned_to="coder")
    _age(p, recovery_env.STALE_THRESHOLD_SECONDS + 60)

    recovery_env.recover_processing_subtasks(_Log())

    dest = fake_project / "agents" / "coder" / "inbox" / "task_s1.task.md"
    assert dest.exists()
    assert not (proc / "task_s1.task.md").exists()
    # Status reset to pending so the worker re-picks it up.
    assert task_io.read_task(dest)["meta"]["status"] == "pending"


def test_fresh_processing_subtask_left_alone(recovery_env, fake_project):
    proc = fake_project / "processing"
    _write_task(proc, "task_s2", status="processing", assigned_to="coder")
    # No backdating — within the normal processing window.

    recovery_env.recover_processing_subtasks(_Log())

    assert (proc / "task_s2.task.md").exists()
    assert not (fake_project / "agents" / "coder" / "inbox" / "task_s2.task.md").exists()


def test_orchestrator_owned_processing_task_skipped(recovery_env, fake_project):
    proc = fake_project / "processing"
    p = _write_task(proc, "task_s3", status="processing", assigned_to="orchestrator")
    _age(p, recovery_env.STALE_THRESHOLD_SECONDS + 60)

    recovery_env.recover_processing_subtasks(_Log())

    # Orchestrator stalls are handled by its own lock, not here.
    assert (proc / "task_s3.task.md").exists()


def test_unknown_worker_processing_subtask_skipped(recovery_env, fake_project):
    proc = fake_project / "processing"
    p = _write_task(proc, "task_s4", status="processing", assigned_to="wizard")
    _age(p, recovery_env.STALE_THRESHOLD_SECONDS + 60)

    log = _Log()
    recovery_env.recover_processing_subtasks(log)

    assert (proc / "task_s4.task.md").exists()
    assert "wizard" in log.text()


# ===========================================================================
# recover_stalled_subtasks — failed/ subtasks whose parent is still alive
# ===========================================================================


def test_stalled_subtask_retried_and_parent_counter_incremented(recovery_env, fake_project):
    proc = fake_project / "processing"
    failed = fake_project / "failed"
    _write_task(proc, "parent1", status="dispatched", assigned_to="orchestrator")
    _write_task(
        failed, "sub1", status="failed", assigned_to="coder",
        parent_task_id="parent1",
    )

    recovery_env.recover_stalled_subtasks(_Log())

    # Subtask returned to coder inbox with status pending
    dest = fake_project / "agents" / "coder" / "inbox" / "sub1.task.md"
    assert dest.exists()
    assert task_io.read_task(dest)["meta"]["status"] == "pending"
    # Parent stays in processing, counter bumped to 1
    parent = task_io.read_task(proc / "parent1.task.md")
    assert parent["meta"]["stall_retry_count"] == 1


def test_stalled_subtask_fails_parent_when_retries_exhausted(recovery_env, fake_project):
    proc = fake_project / "processing"
    failed = fake_project / "failed"
    _write_task(
        proc, "parent2", status="dispatched", assigned_to="orchestrator",
        stall_retry_count=recovery_env.MAX_STALL_RETRIES,
    )
    _write_task(
        failed, "sub2", status="failed", assigned_to="coder",
        parent_task_id="parent2",
    )

    recovery_env.recover_stalled_subtasks(_Log())

    # Parent moved out of processing into failed/
    assert not (proc / "parent2.task.md").exists()
    assert (failed / "parent2.task.md").exists()
    # A failure result file was written to outbox/
    assert (fake_project / "outbox" / "parent2_result.md").exists()


def test_failed_subtask_without_parent_id_skipped(recovery_env, fake_project):
    failed = fake_project / "failed"
    _write_task(failed, "sub3", status="failed", assigned_to="coder")  # no parent_task_id

    log = _Log()
    recovery_env.recover_stalled_subtasks(log)

    assert (failed / "sub3.task.md").exists()
    assert "no parent_task_id" in log.text()


def test_failed_subtask_with_completed_parent_left_alone(recovery_env, fake_project):
    failed = fake_project / "failed"
    outbox = fake_project / "outbox"
    # Parent already complete in outbox/ (not in processing/) → stale, not a stall.
    _write_task(outbox, "parent4", status="complete", assigned_to="orchestrator")
    _write_task(
        failed, "sub4", status="failed", assigned_to="coder",
        parent_task_id="parent4",
    )

    recovery_env.recover_stalled_subtasks(_Log())

    # Untouched — nothing to recover.
    assert (failed / "sub4.task.md").exists()
    assert not (fake_project / "agents" / "coder" / "inbox" / "sub4.task.md").exists()


# ===========================================================================
# recover_orphaned_validation_subtasks — stranded validation/ → outbox/
# ===========================================================================


def test_validation_subtask_swept_when_parent_complete(recovery_env, fake_project):
    validation = fake_project / "validation"
    outbox = fake_project / "outbox"
    _write_task(outbox, "vp1", status="complete", assigned_to="orchestrator")
    _write_task(
        validation, "vsub1", status="awaiting_validation", assigned_to="coder",
        parent_task_id="vp1",
    )

    recovery_env.recover_orphaned_validation_subtasks(_Log())

    assert not (validation / "vsub1.task.md").exists()
    assert (outbox / "vsub1.task.md").exists()


def test_validation_subtask_kept_when_parent_not_complete(recovery_env, fake_project):
    validation = fake_project / "validation"
    proc = fake_project / "processing"
    # Parent still in processing (not complete) → must go through normal loop.
    _write_task(proc, "vp2", status="dispatched", assigned_to="orchestrator")
    _write_task(
        validation, "vsub2", status="awaiting_validation", assigned_to="coder",
        parent_task_id="vp2",
    )

    recovery_env.recover_orphaned_validation_subtasks(_Log())

    assert (validation / "vsub2.task.md").exists()


def test_validation_subtask_no_parent_swept_when_result_exists(recovery_env, fake_project):
    validation = fake_project / "validation"
    outbox = fake_project / "outbox"
    result_path = str(outbox / "vsub3_result.md")
    # No parent_task_id, but the result file already exists on disk → orphaned.
    Path(result_path).write_text("# QA Approval\n", encoding="utf-8")
    _write_task(
        validation, "vsub3", status="awaiting_validation", assigned_to="qa",
        task_type="qa", output_path=result_path,
    )

    recovery_env.recover_orphaned_validation_subtasks(_Log())

    assert not (validation / "vsub3.task.md").exists()
    assert (outbox / "vsub3.task.md").exists()


def test_validation_subtask_no_parent_kept_when_result_missing(recovery_env, fake_project):
    validation = fake_project / "validation"
    outbox = fake_project / "outbox"
    missing = str(outbox / "vsub4_result.md")  # never created
    _write_task(
        validation, "vsub4", status="awaiting_validation", assigned_to="qa",
        task_type="qa", output_path=missing,
    )

    recovery_env.recover_orphaned_validation_subtasks(_Log())

    assert (validation / "vsub4.task.md").exists()
