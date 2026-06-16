"""
Tests for the pure orchestrator helpers in ``scripts/agent_orchestrator.py``.

These functions traverse the filesystem-based task queue to wire QA → coder →
retry-coder chains. They are pure file-IO + frontmatter parsing — no LLM call,
no subprocess — and so are good targets for unit tests.

The ``fake_project`` fixture re-points ``PROJECT_ROOT`` at a temp dir. We also
re-point the orchestrator module's own captured copy of ``PROJECT_ROOT``.
"""

from __future__ import annotations

import os

from pathlib import Path

import frontmatter
import pytest

import shared.task_io as task_io


# ---------------------------------------------------------------------------
# Fixture: import the orchestrator and re-point PROJECT_ROOT
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator(fake_project, monkeypatch):
    """Import agent_orchestrator and re-point its PROJECT_ROOT to the fake tree."""
    import agent_orchestrator as ao
    monkeypatch.setattr(ao, "PROJECT_ROOT", fake_project)
    return ao


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_qa_task(
    folder: Path,
    qa_id: str,
    coder_output: str,
    retry_count: int = 0,
    created_at: str = "2026-01-01T10:00:00",
    output_path: str = "",
) -> Path:
    """Write a QA-type task that references `coder_output` in its context_files."""
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": qa_id,
        "type": "qa",
        "priority": "medium",
        "created_by": "coder",
        "created_at": created_at,
        "assigned_to": "qa",
        "status": "pending",
        "context_files": [coder_output],
        "output_path": output_path or f"outbox/{qa_id}_result.md",
        "retry_count": retry_count,
    }
    post = frontmatter.Post("## QA Task\n", **meta)
    path = folder / f"{qa_id}.task.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _write_retry_coder_task(
    folder: Path,
    coder_id: str,
    created_at: str,
    created_by: str = "qa",
    output_path: str = "",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": coder_id,
        "type": "code",
        "priority": "medium",
        "created_by": created_by,
        "created_at": created_at,
        "assigned_to": "coder",
        "status": "pending",
        "output_path": output_path or f"outbox/{coder_id}_result.md",
        "context_files": [],
    }
    post = frontmatter.Post("## Retry Task\n", **meta)
    path = folder / f"{coder_id}.task.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _find_qa_for_output
# ---------------------------------------------------------------------------


def test_find_qa_for_output_returns_not_found_when_no_qa_task(orchestrator):
    status, qa = orchestrator._find_qa_for_output("outbox/missing_result.md")
    assert status == "not_found"
    assert qa is None


def test_find_qa_for_output_finds_pending_qa_in_qa_inbox(orchestrator, fake_project):
    coder_out = str(fake_project / "outbox" / "coder_out_result.md")
    qa_path = _write_qa_task(
        fake_project / "agents" / "qa" / "inbox",
        qa_id="task_qa_001",
        coder_output=coder_out,
    )

    status, qa = orchestrator._find_qa_for_output(coder_out)
    assert status == "pending"
    assert qa["meta"]["id"] == "task_qa_001"


def test_find_qa_for_output_finds_pending_qa_in_processing(orchestrator, fake_project):
    coder_out = str(fake_project / "outbox" / "coder_out_result.md")
    _write_qa_task(fake_project / "processing", qa_id="task_qa_002", coder_output=coder_out)

    status, qa = orchestrator._find_qa_for_output(coder_out)
    assert status == "pending"


def test_find_qa_for_output_finds_done_qa_in_validation(orchestrator, fake_project):
    coder_out = str(fake_project / "outbox" / "coder_out_result.md")
    _write_qa_task(fake_project / "validation", qa_id="task_qa_003", coder_output=coder_out)

    status, qa = orchestrator._find_qa_for_output(coder_out)
    assert status == "done"
    assert qa["meta"]["id"] == "task_qa_003"


def test_find_qa_for_output_finds_done_qa_in_outbox(orchestrator, fake_project):
    coder_out = str(fake_project / "outbox" / "coder_out_result.md")
    _write_qa_task(fake_project / "outbox", qa_id="task_qa_004", coder_output=coder_out)

    status, qa = orchestrator._find_qa_for_output(coder_out)
    assert status == "done"


def test_find_qa_for_output_ignores_non_qa_tasks(orchestrator, fake_project):
    """A coder-type task with matching context_files must not be confused for QA."""
    coder_out = str(fake_project / "outbox" / "coder_out_result.md")
    # Write a non-qa task that *also* references the output
    inbox = fake_project / "agents" / "qa" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": "task_code_999",
        "type": "code",  # not qa
        "context_files": [coder_out],
        "status": "pending",
    }
    post = frontmatter.Post("body", **meta)
    (inbox / "task_code_999.task.md").write_text(frontmatter.dumps(post), encoding="utf-8")

    status, qa = orchestrator._find_qa_for_output(coder_out)
    assert status == "not_found"
    assert qa is None


def test_find_qa_for_output_matches_by_basename_only(orchestrator, fake_project):
    """QA's context_files may store absolute or relative paths — basename match.

    The helper uses ``pathlib.Path(cf).name``, which only splits on the host
    OS's separator (``\\`` on Windows, ``/`` on POSIX). To keep the test
    meaningful on whichever platform CI runs, use an absolute path that uses
    the host's own separator rather than hard-coding a Windows ``C:\\`` path
    (which ``PurePosixPath`` would not split, failing on Linux).
    """
    coder_out = "outbox/coder_relative_result.md"
    abs_coder_output = (
        r"C:\some\abs\path\coder_relative_result.md"
        if os.name == "nt"
        else "/some/abs/path/coder_relative_result.md"
    )
    _write_qa_task(
        fake_project / "validation",
        qa_id="task_qa_005",
        coder_output=abs_coder_output,
    )

    status, qa = orchestrator._find_qa_for_output(coder_out)
    assert status == "done"


def test_find_qa_for_output_handles_missing_folders(orchestrator, fake_project):
    """Helper must not crash if a pipeline folder doesn't exist."""
    # Remove validation folder
    import shutil
    shutil.rmtree(fake_project / "validation")
    status, qa = orchestrator._find_qa_for_output("anything")
    assert status == "not_found"


# ---------------------------------------------------------------------------
# _extract_qa_verdict
# ---------------------------------------------------------------------------


def test_extract_qa_verdict_pass(orchestrator, fake_project):
    out = fake_project / "outbox" / "qa_result.md"
    out.write_text("Some review.\nVerdict: PASS\n", encoding="utf-8")
    qa_task = {"meta": {"output_path": str(out)}}
    assert orchestrator._extract_qa_verdict(qa_task) == "PASS"


def test_extract_qa_verdict_fail(orchestrator, fake_project):
    out = fake_project / "outbox" / "qa_result.md"
    out.write_text("Verdict: FAIL — missing edge case\n", encoding="utf-8")
    qa_task = {"meta": {"output_path": str(out)}}
    assert orchestrator._extract_qa_verdict(qa_task) == "FAIL"


def test_extract_qa_verdict_unknown_when_neither_keyword(orchestrator, fake_project):
    out = fake_project / "outbox" / "qa_result.md"
    out.write_text("just text without a verdict\n", encoding="utf-8")
    qa_task = {"meta": {"output_path": str(out)}}
    assert orchestrator._extract_qa_verdict(qa_task) == "UNKNOWN"


def test_extract_qa_verdict_unknown_when_file_missing(orchestrator, fake_project):
    qa_task = {"meta": {"output_path": str(fake_project / "outbox" / "missing.md")}}
    assert orchestrator._extract_qa_verdict(qa_task) == "UNKNOWN"


def test_extract_qa_verdict_unknown_when_no_output_path(orchestrator):
    qa_task = {"meta": {}}
    assert orchestrator._extract_qa_verdict(qa_task) == "UNKNOWN"


# ---------------------------------------------------------------------------
# _find_retry_coder_output — QA-dispatched retry coder discovery
# ---------------------------------------------------------------------------


def test_find_retry_coder_output_returns_none_when_no_qa_id(orchestrator):
    qa_task = {"meta": {}}
    assert orchestrator._find_retry_coder_output(qa_task) is None


def test_find_retry_coder_output_returns_none_when_no_retry_task_yet(orchestrator):
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}
    assert orchestrator._find_retry_coder_output(qa_task) is None


def test_find_retry_coder_output_returns_none_when_retry_still_inflight(orchestrator, fake_project):
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}
    # Retry coder is in agents/coder/inbox (in-flight)
    _write_retry_coder_task(
        fake_project / "agents" / "coder" / "inbox",
        coder_id="task_retry_a",
        created_at="2026-01-01T10:05:00",
    )

    # In-flight returns None (still running, not ready)
    assert orchestrator._find_retry_coder_output(qa_task) is None


def test_find_retry_coder_output_returns_path_when_complete(orchestrator, fake_project):
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}
    retry_id = "task_retry_a"
    output_file = fake_project / "outbox" / f"{retry_id}_result.md"
    output_file.write_text("retry result", encoding="utf-8")
    _write_retry_coder_task(
        fake_project / "outbox",
        coder_id=retry_id,
        created_at="2026-01-01T10:05:00",
        output_path=str(output_file),
    )

    result = orchestrator._find_retry_coder_output(qa_task)
    assert result == str(output_file)


def test_find_retry_coder_output_returns_sentinel_when_failed(orchestrator, fake_project):
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}
    _write_retry_coder_task(
        fake_project / "failed",
        coder_id="task_retry_failed",
        created_at="2026-01-01T10:05:00",
    )
    result = orchestrator._find_retry_coder_output(qa_task)
    assert result is orchestrator._RETRY_CODER_FAILED


def test_find_retry_coder_output_skips_tasks_created_before_qa(orchestrator, fake_project):
    """Timestamp guard: only retry tasks created AT or AFTER the QA task qualify."""
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}

    # An older coder task with `created_by: qa` from a previous chain
    _write_retry_coder_task(
        fake_project / "outbox",
        coder_id="task_retry_old",
        created_at="2025-12-31T23:00:00",
    )

    result = orchestrator._find_retry_coder_output(qa_task)
    assert result is None


def test_find_retry_coder_output_ignores_tasks_not_created_by_qa(orchestrator, fake_project):
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}

    # Coder task created by orchestrator (not qa) — should be ignored
    _write_retry_coder_task(
        fake_project / "outbox",
        coder_id="task_unrelated",
        created_at="2026-01-01T10:05:00",
        created_by="orchestrator",
    )

    assert orchestrator._find_retry_coder_output(qa_task) is None


def test_find_retry_coder_output_handles_missing_directories(orchestrator, fake_project):
    qa_task = {"meta": {"id": "task_qa_a", "created_at": "2026-01-01T10:00:00"}}
    import shutil
    shutil.rmtree(fake_project / "outbox")
    shutil.rmtree(fake_project / "failed")
    # Must not crash
    assert orchestrator._find_retry_coder_output(qa_task) is None


# ---------------------------------------------------------------------------
# _find_qa_for_coder_subtask — the higher-level wrapper
# ---------------------------------------------------------------------------


def test_find_qa_for_coder_subtask_returns_not_found_when_no_output_path(orchestrator):
    coder = {"meta": {}}
    status, qa = orchestrator._find_qa_for_coder_subtask(coder)
    assert status == "not_found"
    assert qa is None


def test_find_qa_for_coder_subtask_returns_pending_when_qa_inflight(orchestrator, fake_project):
    coder_out = str(fake_project / "outbox" / "coder_x_result.md")
    coder = {"meta": {"output_path": coder_out}}

    _write_qa_task(
        fake_project / "agents" / "qa" / "inbox",
        qa_id="task_qa_inflight",
        coder_output=coder_out,
    )

    status, qa = orchestrator._find_qa_for_coder_subtask(coder)
    assert status == "pending"


def test_find_qa_for_coder_subtask_returns_done_when_qa_passes(orchestrator, fake_project):
    coder_out = str(fake_project / "outbox" / "coder_y_result.md")
    coder = {"meta": {"output_path": coder_out}}

    qa_out = fake_project / "outbox" / "task_qa_pass_result.md"
    qa_out.write_text("Verdict: PASS\n", encoding="utf-8")
    _write_qa_task(
        fake_project / "validation",
        qa_id="task_qa_pass",
        coder_output=coder_out,
        retry_count=0,
        output_path=str(qa_out),
    )

    status, qa = orchestrator._find_qa_for_coder_subtask(coder)
    assert status == "done"
    assert qa["meta"]["id"] == "task_qa_pass"


# ---------------------------------------------------------------------------
# load_system_prompt — error handling when file missing
# ---------------------------------------------------------------------------


def test_load_system_prompt_raises_when_file_missing(orchestrator, monkeypatch, fake_project):
    # System prompt path is captured at module load; re-point at a missing file
    monkeypatch.setattr(
        orchestrator,
        "SYSTEM_PROMPT_PATH",
        fake_project / "agents" / "orchestrator" / "missing.md",
    )
    with pytest.raises(FileNotFoundError):
        orchestrator.load_system_prompt()


def test_load_system_prompt_reads_file(orchestrator, fake_project, monkeypatch):
    prompt_file = fake_project / "agents" / "orchestrator" / "system_prompt.md"
    prompt_file.write_text("# Orchestrator\nHi.", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "SYSTEM_PROMPT_PATH", prompt_file)
    assert "Orchestrator" in orchestrator.load_system_prompt()
# (end of test module)
