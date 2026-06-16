"""
Forward-path hardening tests:

  * ``task_io._atomic_write_text`` — writes are all-or-nothing and leave no
    temp file behind.
  * ``agent_coder`` coder→QA chain — creates exactly one QA task and advances
    to validation/; is idempotent if a QA for the output already exists; and
    refuses to advance to validation/ if the QA task is not on disk (so a
    coder subtask can never reach validation/ without its QA, which would stall
    the validation gate).
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

import shared.task_io as task_io
from shared.task_io import create_task_file, read_task


class _Log:
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass
    def debug(self, m): pass


class _CoderClient:
    """Returns a canned code block; records nothing else."""
    last_token_counts = {"prompt": 0, "completion": 0}

    def chat(self, *a, **k):
        return "Here you go:\n```python\nprint('hello')\n```"


# ---------------------------------------------------------------------------
# _atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_and_no_tmp(tmp_path):
    target = tmp_path / "out.md"
    task_io._atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"
    # No leftover *.tmp siblings.
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "out.md"
    target.write_text("old", encoding="utf-8")
    task_io._atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_result_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(task_io, "PROJECT_ROOT", tmp_path)
    out = tmp_path / "outbox" / "r.md"
    task_io.write_result(str(out), "body", meta={"task_id": "t", "status": "complete"})
    assert read_task(out)["meta"]["status"] == "complete"
    assert list((tmp_path / "outbox").glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# agent_coder coder→QA chain
# ---------------------------------------------------------------------------


@pytest.fixture
def coder(fake_project, monkeypatch):
    import agent_coder
    monkeypatch.setattr(agent_coder, "PROJECT_ROOT", fake_project)
    (fake_project / "agents" / "coder" / "system_prompt.md").write_text("c", encoding="utf-8")
    # Avoid the RAG network call in build_user_message.
    monkeypatch.setattr("shared.agent_boilerplate.inject_rag_context", lambda body, char_limit=500: body)
    return agent_coder


def _make_coder_task(fake_project):
    path = create_task_file(
        inbox_path=fake_project / "agents" / "coder" / "inbox",
        task_type="code",
        description="write a hello",
        expected_output="a script",
        assigned_to="coder",
        created_by="test",
        chain_to="qa",
        original_description="write a hello",
    )
    return path


def _qa_inbox_files(fake_project):
    return list((fake_project / "agents" / "qa" / "inbox").glob("*.task.md"))


def test_coder_creates_one_qa_and_advances_to_validation(coder, fake_project):
    task_path = _make_coder_task(fake_project)
    coder.process_task(read_task(task_path), _CoderClient(), _Log())

    # Exactly one QA task created, referencing the coder output.
    qa_files = _qa_inbox_files(fake_project)
    assert len(qa_files) == 1
    qa_meta = read_task(qa_files[0])["meta"]
    assert qa_meta["type"] == "qa"
    assert Path(qa_meta["context_files"][0]).name.endswith("_result.md")

    # Coder subtask advanced to validation/, not left in processing/.
    assert (fake_project / "validation" / task_path.name).exists()
    assert not (fake_project / "processing" / task_path.name).exists()


def test_coder_chain_is_idempotent_when_qa_already_exists(coder, fake_project):
    coder_task_path = _make_coder_task(fake_project)
    task = read_task(coder_task_path)
    output_path = task["meta"]["output_path"]

    # Simulate "QA already created on a prior (crashed) run": a QA task that
    # references this coder output already sits in qa/inbox.
    pre_meta = {
        "id": "task_qa_pre",
        "type": "qa",
        "created_at": "2026-01-01T10:00:00",
        "assigned_to": "qa",
        "status": "pending",
        "context_files": [output_path],
        "output_path": "outbox/task_qa_pre_result.md",
    }
    qa_dir = fake_project / "agents" / "qa" / "inbox"
    (qa_dir / "task_qa_pre.task.md").write_text(
        frontmatter.dumps(frontmatter.Post("qa", **pre_meta)), encoding="utf-8"
    )

    coder.process_task(task, _CoderClient(), _Log())

    # No duplicate QA — still just the pre-existing one.
    qa_files = _qa_inbox_files(fake_project)
    assert len(qa_files) == 1
    assert qa_files[0].name == "task_qa_pre.task.md"
    # And the coder still advanced to validation/.
    assert (fake_project / "validation" / coder_task_path.name).exists()
    assert not (fake_project / "processing" / coder_task_path.name).exists()


def test_coder_stays_in_processing_if_qa_not_on_disk(coder, fake_project, monkeypatch):
    task_path = _make_coder_task(fake_project)

    # Simulate QA creation that does not actually land on disk.
    monkeypatch.setattr(
        coder, "create_task_file",
        lambda **kw: Path(fake_project / "agents" / "qa" / "inbox" / "ghost.task.md"),
    )

    coder.process_task(read_task(task_path), _CoderClient(), _Log())

    # Verify-before-advance: coder must NOT reach validation/ without its QA.
    assert (fake_project / "processing" / task_path.name).exists()
    assert not (fake_project / "validation" / task_path.name).exists()
