"""
Tests for ``scripts/shared/task_io.py``.

Coverage focus:
  - frontmatter round-trip via ``read_task`` / ``write_result``
  - ``mark_processing`` / ``mark_awaiting_validation`` / ``mark_completed``
    preserve all original frontmatter fields (the frontmatter round-trip regression)
  - ``safe_read_context`` rejects path traversal, missing files, directories
  - ``create_task_file`` populates frontmatter + body correctly
  - ``resolve_task_dependencies`` wires completed deps into context_files
  - ``get_completed_subtasks_by_parent`` groups by parent_task_id
"""

from __future__ import annotations

import logging
from pathlib import Path

import frontmatter
import pytest

import shared.task_io as task_io


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_task_file(path: Path, meta: dict, body: str = "## Task\n\nDo a thing.\n") -> Path:
    """Write a `.task.md` file with the supplied frontmatter and body."""
    post = frontmatter.Post(body, **meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# read_task / write_result
# ---------------------------------------------------------------------------


def test_read_task_returns_meta_body_path(fake_project, sample_task_meta):
    task_file = fake_project / "inbox" / "t.task.md"
    _write_task_file(task_file, sample_task_meta, body="hello world\n")

    result = task_io.read_task(task_file)

    assert result["meta"]["id"] == sample_task_meta["id"]
    assert result["meta"]["type"] == "code"
    assert "hello world" in result["body"]
    assert result["path"] == task_file


def test_read_task_preserves_all_frontmatter_keys(fake_project, sample_task_meta):
    """Round-trip must not drop unknown fields — agents add custom keys."""
    sample_task_meta["parent_task_id"] = "task_parent_123"
    sample_task_meta["depends_on"] = ["task_dep_1", "task_dep_2"]
    sample_task_meta["chain_to"] = "qa"
    sample_task_meta["retry_count"] = 2
    task_file = fake_project / "inbox" / "t.task.md"
    _write_task_file(task_file, sample_task_meta)

    result = task_io.read_task(task_file)
    assert result["meta"]["parent_task_id"] == "task_parent_123"
    assert result["meta"]["depends_on"] == ["task_dep_1", "task_dep_2"]
    assert result["meta"]["chain_to"] == "qa"
    assert result["meta"]["retry_count"] == 2


def test_write_result_with_meta(fake_project):
    out_path = fake_project / "outbox" / "result.md"
    task_io.write_result(out_path, "# Result\n\nbody.\n", meta={"id": "x", "status": "complete"})

    raw = out_path.read_text(encoding="utf-8")
    assert raw.startswith("---")
    parsed = frontmatter.loads(raw)
    assert parsed["id"] == "x"
    assert parsed["status"] == "complete"
    assert "body." in parsed.content


def test_write_result_without_meta_is_plain_text(fake_project):
    out_path = fake_project / "outbox" / "plain.md"
    task_io.write_result(out_path, "no frontmatter")
    assert out_path.read_text(encoding="utf-8") == "no frontmatter"


# ---------------------------------------------------------------------------
# safe_read_context — the path-traversal defense
# ---------------------------------------------------------------------------


def test_safe_read_context_returns_content_for_in_project_file(fake_project):
    f = fake_project / "context" / "doc.md"
    f.write_text("hello", encoding="utf-8")

    assert task_io.safe_read_context(str(f)) == "hello"


def test_safe_read_context_rejects_traversal_outside_root(fake_project, caplog):
    # Absolute path outside the project
    hostile = "C:\\Windows\\System32\\drivers\\etc\\hosts" if Path("/").drive else "/etc/passwd"
    with caplog.at_level(logging.WARNING):
        result = task_io.safe_read_context(hostile, logger=logging.getLogger("test"))
    assert result is None
    assert any("outside project root" in rec.message for rec in caplog.records)


def test_safe_read_context_rejects_relative_traversal(fake_project, caplog):
    # `..` traversal — resolves outside the project root
    relative_evil = str(fake_project.parent / "evil.txt")
    Path(relative_evil).write_text("oops", encoding="utf-8")
    try:
        with caplog.at_level(logging.WARNING):
            result = task_io.safe_read_context(relative_evil, logger=logging.getLogger("test"))
        assert result is None
        assert any("outside project root" in rec.message for rec in caplog.records)
    finally:
        Path(relative_evil).unlink(missing_ok=True)


def test_safe_read_context_returns_none_for_empty_input():
    assert task_io.safe_read_context("") is None
    assert task_io.safe_read_context(None) is None


def test_safe_read_context_returns_none_for_missing_file(fake_project, caplog):
    missing = str(fake_project / "context" / "nope.md")
    with caplog.at_level(logging.WARNING):
        result = task_io.safe_read_context(missing, logger=logging.getLogger("test"))
    assert result is None
    assert any("not found" in rec.message for rec in caplog.records)


def test_safe_read_context_rejects_directory(fake_project, caplog):
    d = fake_project / "context"  # a real directory
    with caplog.at_level(logging.WARNING):
        result = task_io.safe_read_context(str(d), logger=logging.getLogger("test"))
    assert result is None
    assert any("not a regular file" in rec.message for rec in caplog.records)


def test_safe_read_context_silent_without_logger(fake_project):
    """Helper must not crash when called without a logger."""
    # Outside root, no logger — should just return None
    assert task_io.safe_read_context("/no/such/file.txt") is None


# ---------------------------------------------------------------------------
# mark_processing — preserves all original frontmatter fields (frontmatter round-trip bug)
# ---------------------------------------------------------------------------


def test_mark_processing_preserves_all_fields(fake_project, sample_task_meta):
    sample_task_meta["parent_task_id"] = "task_parent_99"
    sample_task_meta["depends_on"] = ["a", "b"]
    sample_task_meta["chain_to"] = "qa"
    sample_task_meta["output_path"] = "outbox/x_result.md"
    task_file = fake_project / "inbox" / "t.task.md"
    _write_task_file(task_file, sample_task_meta)

    new_path = task_io.mark_processing(task_file)

    # File moved to processing/
    assert new_path.parent.name == "processing"
    assert not task_file.exists()
    # All original fields survive the round-trip
    moved = task_io.read_task(new_path)
    assert moved["meta"]["id"] == sample_task_meta["id"]
    assert moved["meta"]["parent_task_id"] == "task_parent_99"
    assert moved["meta"]["depends_on"] == ["a", "b"]
    assert moved["meta"]["chain_to"] == "qa"
    assert moved["meta"]["output_path"] == "outbox/x_result.md"
    # status flipped to "processing"
    assert moved["meta"]["status"] == "processing"


def test_mark_processing_handles_windows_path_in_output_path(fake_project, sample_task_meta):
    """Regression: backslash-laden Windows paths used to break the round-trip."""
    sample_task_meta["output_path"] = r"C:\Users\X\outbox\result.md"
    task_file = fake_project / "inbox" / "t.task.md"
    _write_task_file(task_file, sample_task_meta)

    new_path = task_io.mark_processing(task_file)
    moved = task_io.read_task(new_path)
    assert moved["meta"]["output_path"] == r"C:\Users\X\outbox\result.md"
    assert moved["meta"]["status"] == "processing"


def test_mark_awaiting_validation_sets_status_and_moves(fake_project, sample_task_meta):
    task_file = fake_project / "processing" / "t.task.md"
    _write_task_file(task_file, sample_task_meta)

    new_path = task_io.mark_awaiting_validation(task_file)
    assert new_path.parent.name == "validation"
    assert task_io.read_task(new_path)["meta"]["status"] == "awaiting_validation"


def test_mark_completed_writes_status_complete(fake_project, sample_task_meta):
    task_file = fake_project / "validation" / "t.task.md"
    _write_task_file(task_file, sample_task_meta)

    new_path = task_io.mark_completed(task_file)
    assert new_path.parent.name == "outbox"
    assert task_io.read_task(new_path)["meta"]["status"] == "complete"


def test_mark_failed_moves_to_failed(fake_project, sample_task_meta):
    task_file = fake_project / "processing" / "t.task.md"
    _write_task_file(task_file, sample_task_meta)

    new_path = task_io.mark_failed(task_file)
    assert new_path.parent.name == "failed"
    assert new_path.exists()


# ---------------------------------------------------------------------------
# create_task_file
# ---------------------------------------------------------------------------


def test_create_task_file_basic(fake_project):
    inbox = fake_project / "agents" / "coder" / "inbox"
    task_path = task_io.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="Build a thing.",
        expected_output="A working thing.",
    )
    assert task_path.parent == inbox
    task = task_io.read_task(task_path)
    assert task["meta"]["type"] == "code"
    assert task["meta"]["status"] == "pending"
    assert task["meta"]["context_files"] == []
    assert "Build a thing." in task["body"]
    assert "A working thing." in task["body"]


def test_create_task_file_with_validation_context_injects_body_section(fake_project):
    inbox = fake_project / "agents" / "coder" / "inbox"
    task_path = task_io.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="Try again.",
        expected_output="Better code.",
        validation_context={"decision_type": "refine", "reasoning": "missing edge case"},
    )
    task = task_io.read_task(task_path)
    assert task["meta"]["validation_context"]["decision_type"] == "refine"
    assert "## Validation Context" in task["body"]
    assert "refine" in task["body"]
    assert "missing edge case" in task["body"]


def test_create_task_file_optional_fields_only_added_when_set(fake_project):
    inbox = fake_project / "agents" / "coder" / "inbox"
    task_path = task_io.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="d",
        expected_output="e",
        chain_to="qa",
        parent_task_id="task_parent_1",
        depends_on=["task_dep_1"],
        retry_count=1,
    )
    task = task_io.read_task(task_path)
    assert task["meta"]["chain_to"] == "qa"
    assert task["meta"]["parent_task_id"] == "task_parent_1"
    assert task["meta"]["depends_on"] == ["task_dep_1"]
    assert task["meta"]["retry_count"] == 1


def test_create_task_file_omits_optional_when_not_set(fake_project):
    inbox = fake_project / "agents" / "coder" / "inbox"
    task_path = task_io.create_task_file(
        inbox_path=inbox,
        task_type="code",
        description="d",
        expected_output="e",
    )
    task = task_io.read_task(task_path)
    # Should NOT have these keys
    assert "chain_to" not in task["meta"]
    assert "parent_task_id" not in task["meta"]
    assert "depends_on" not in task["meta"]
    assert "retry_count" not in task["meta"]
    assert "validation_context" not in task["meta"]


# ---------------------------------------------------------------------------
# generate_task_id
# ---------------------------------------------------------------------------


def test_generate_task_id_format():
    tid = task_io.generate_task_id()
    # task_YYYYMMDD_HHMMSS_microseconds
    parts = tid.split("_")
    assert parts[0] == "task"
    assert len(parts[1]) == 8 and parts[1].isdigit()
    assert len(parts[2]) == 6 and parts[2].isdigit()
    assert parts[3].isdigit()


def test_generate_task_id_includes_microseconds_field():
    """The microseconds suffix is what protects against same-second collisions.
    We can't reliably test uniqueness across calls (Windows clock resolution),
    but we can verify the suffix exists and varies across enough calls."""
    import time as _time
    ids = []
    for _ in range(5):
        ids.append(task_io.generate_task_id())
        _time.sleep(0.001)  # 1ms — well above microsecond resolution
    # With 1ms gaps, the microsecond fields should differ
    suffixes = {tid.rsplit("_", 1)[-1] for tid in ids}
    assert len(suffixes) >= 4


# ---------------------------------------------------------------------------
# list_pending_tasks / list_validation_tasks
# ---------------------------------------------------------------------------


def test_list_pending_tasks_returns_sorted(fake_project, sample_task_meta):
    inbox = fake_project / "inbox"
    _write_task_file(inbox / "b.task.md", sample_task_meta)
    _write_task_file(inbox / "a.task.md", sample_task_meta)
    # not a task file — should be ignored
    (inbox / "readme.md").write_text("ignore", encoding="utf-8")

    tasks = task_io.list_pending_tasks(inbox)
    assert [t.name for t in tasks] == ["a.task.md", "b.task.md"]


def test_list_validation_tasks_handles_missing_dir(fake_project):
    nonexistent = fake_project / "nope"
    assert task_io.list_validation_tasks(nonexistent) == []


# ---------------------------------------------------------------------------
# get_completed_subtasks_by_parent
# ---------------------------------------------------------------------------


def test_get_completed_subtasks_by_parent_groups_correctly(fake_project, sample_task_meta):
    validation = fake_project / "validation"

    s1 = dict(sample_task_meta, id="task_s_1", parent_task_id="parent_a")
    s2 = dict(sample_task_meta, id="task_s_2", parent_task_id="parent_a")
    s3 = dict(sample_task_meta, id="task_s_3", parent_task_id="parent_b")
    # no parent — should be dropped
    s4 = dict(sample_task_meta, id="task_s_4")
    s4.pop("parent_task_id", None)

    _write_task_file(validation / "s1.task.md", s1)
    _write_task_file(validation / "s2.task.md", s2)
    _write_task_file(validation / "s3.task.md", s3)
    _write_task_file(validation / "s4.task.md", s4)

    grouped = task_io.get_completed_subtasks_by_parent(validation)
    assert set(grouped.keys()) == {"parent_a", "parent_b"}
    assert len(grouped["parent_a"]) == 2
    assert len(grouped["parent_b"]) == 1


def test_get_completed_subtasks_by_parent_empty(fake_project):
    grouped = task_io.get_completed_subtasks_by_parent(fake_project / "validation")
    assert grouped == {}


# ---------------------------------------------------------------------------
# read_subtask_result
# ---------------------------------------------------------------------------


def test_read_subtask_result_returns_content(fake_project):
    result_file = fake_project / "outbox" / "result.md"
    result_file.write_text("hello", encoding="utf-8")
    assert task_io.read_subtask_result(str(result_file)) == "hello"


def test_read_subtask_result_returns_marker_when_missing(fake_project):
    missing = str(fake_project / "outbox" / "nope.md")
    out = task_io.read_subtask_result(missing)
    assert "Result file not found" in out


# ---------------------------------------------------------------------------
# resolve_task_dependencies
# ---------------------------------------------------------------------------


def test_resolve_task_dependencies_unblocks_when_deps_complete(fake_project, sample_task_meta):
    coder_inbox = fake_project / "agents" / "coder" / "inbox"
    outbox = fake_project / "outbox"

    # Set up completed dep in outbox/: a task file + its result file
    dep_id = "task_dep_001"
    dep_result = outbox / f"{dep_id}_result.md"
    dep_result.write_text("dep content", encoding="utf-8")
    dep_task_meta = dict(sample_task_meta, id=dep_id, status="complete",
                          output_path=str(dep_result))
    _write_task_file(outbox / f"{dep_id}.task.md", dep_task_meta)

    # Pending coder task that depends on the above
    waiting = dict(sample_task_meta, id="task_w_001", depends_on=[dep_id])
    waiting_path = coder_inbox / "w.task.md"
    _write_task_file(waiting_path, waiting)

    task_io.resolve_task_dependencies({"coder": coder_inbox})

    after = task_io.read_task(waiting_path)
    assert "depends_on" not in after["meta"]
    assert str(dep_result) in after["meta"]["context_files"]


def test_resolve_task_dependencies_leaves_blocked_when_dep_missing(fake_project, sample_task_meta):
    coder_inbox = fake_project / "agents" / "coder" / "inbox"

    waiting = dict(sample_task_meta, id="task_w_002", depends_on=["task_missing"])
    waiting_path = coder_inbox / "w.task.md"
    _write_task_file(waiting_path, waiting)

    task_io.resolve_task_dependencies({"coder": coder_inbox})

    after = task_io.read_task(waiting_path)
    assert after["meta"]["depends_on"] == ["task_missing"]


def test_resolve_task_dependencies_skips_tasks_without_deps(fake_project, sample_task_meta):
    coder_inbox = fake_project / "agents" / "coder" / "inbox"
    no_deps_path = coder_inbox / "no_deps.task.md"
    _write_task_file(no_deps_path, sample_task_meta)
    original = no_deps_path.read_text(encoding="utf-8")

    task_io.resolve_task_dependencies({"coder": coder_inbox})

    # File unchanged
    assert no_deps_path.read_text(encoding="utf-8") == original
