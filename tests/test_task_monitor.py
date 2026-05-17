"""
Tests for ``dashboard/task_monitor.py`` — focused on the frontmatter parser
(M5 regression net) and end-to-end task detection via the fake project tree.

The dashboard previously used a hand-rolled split-on-colon parser that
silently dropped colons-in-values, couldn't read lists (context_files),
and couldn't read nested dicts (validation_context). These tests lock in
the post-M5 behavior so any future regression is caught immediately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import frontmatter
import pytest

# Add dashboard/ to sys.path for `from task_monitor import TaskMonitor`
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from task_monitor import TaskMonitor  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def monitor(fake_project):
    return TaskMonitor(fake_project)


# ---------------------------------------------------------------------------
# _parse_yaml_frontmatter — the M5 unit
# ---------------------------------------------------------------------------


def test_parser_returns_empty_dict_for_empty_input(monitor):
    assert monitor._parse_yaml_frontmatter("") == {}


def test_parser_returns_empty_dict_for_whitespace(monitor):
    assert monitor._parse_yaml_frontmatter("   \n  \n") == {}


def test_parser_returns_empty_dict_for_bare_scalar(monitor):
    """A non-mapping YAML doc (just a scalar) must not crash callers."""
    assert monitor._parse_yaml_frontmatter("just a string") == {}


def test_parser_returns_empty_dict_on_malformed_yaml(monitor):
    """yaml.YAMLError is caught — never propagates to the caller."""
    bad = "id: task_001\n  : invalid indent"
    assert monitor._parse_yaml_frontmatter(bad) == {}


def test_parser_reads_simple_string_fields(monitor):
    fm = "id: task_001\ntype: code\npriority: high\n"
    md = monitor._parse_yaml_frontmatter(fm)
    assert md == {"id": "task_001", "type": "code", "priority": "high"}


def test_parser_preserves_windows_path_unquoted(monitor):
    """The original motivation for the hand-rolled parser was a bug
    on Windows paths with backslashes. yaml.safe_load handles these
    fine because the backslash only has escape meaning inside
    *double-quoted* strings — bare scalars treat \\ as a literal."""
    fm = r"output_path: C:\Users\X\file.md"
    md = monitor._parse_yaml_frontmatter(fm)
    assert md["output_path"] == r"C:\Users\X\file.md"


def test_parser_returns_int_for_numeric_fields(monitor):
    """retry_count and iteration must be ints, not strings — the
    dashboard sometimes uses them in arithmetic."""
    fm = "retry_count: 3\niteration: 2\n"
    md = monitor._parse_yaml_frontmatter(fm)
    assert md["retry_count"] == 3
    assert isinstance(md["retry_count"], int)
    assert md["iteration"] == 2


def test_parser_reads_list_values(monitor):
    """The old parser couldn't read YAML block-style lists at all —
    context_files would silently become empty."""
    fm = (
        "context_files:\n"
        "  - C:\\Users\\X\\file1.md\n"
        "  - outbox/y_result.md\n"
    )
    md = monitor._parse_yaml_frontmatter(fm)
    assert md["context_files"] == ["C:\\Users\\X\\file1.md", "outbox/y_result.md"]


def test_parser_reads_nested_dict_values(monitor):
    """validation_context is a nested dict — the old parser couldn't
    even see it (the parent line had no value after :)."""
    fm = (
        "validation_context:\n"
        "  decision_type: refine\n"
        "  reasoning: missing edge case\n"
    )
    md = monitor._parse_yaml_frontmatter(fm)
    assert md["validation_context"] == {
        "decision_type": "refine",
        "reasoning": "missing edge case",
    }


def test_parser_preserves_colons_inside_quoted_values(monitor):
    """The old parser split on the first colon, dropping everything after the
    colon-in-value. This was a silent data-loss bug."""
    fm = "description: 'Build a thing: with colons in it'\n"
    md = monitor._parse_yaml_frontmatter(fm)
    assert md["description"] == "Build a thing: with colons in it"


def test_parser_ignores_yaml_comments(monitor):
    fm = (
        "# this is a comment\n"
        "id: task_005\n"
        "# another comment\n"
        "type: code\n"
    )
    md = monitor._parse_yaml_frontmatter(fm)
    assert md == {"id": "task_005", "type": "code"}


def test_parser_round_trips_real_task_file(monitor, fake_project):
    """End-to-end: use python-frontmatter (what task_io.py writes) to produce
    a real task file, then parse the frontmatter block with the dashboard's
    parser. Every field must survive."""
    meta = {
        "id": "task_round_001",
        "type": "code",
        "priority": "medium",
        "created_by": "claude-cowork",
        "created_at": "2026-05-17T12:00:00",
        "assigned_to": "coder",
        "status": "pending",
        "output_path": r"C:\Users\JAAS\Desktop\AI Team\outbox\x_result.md",
        "context_files": [r"C:\Users\X\file1.md", "outbox/y_result.md"],
        "retry_count": 3,
        "parent_task_id": "task_parent_999",
        "chain_to": "qa",
    }
    post = frontmatter.Post("# body\n", **meta)
    serialized = frontmatter.dumps(post)
    # Mimic what task_monitor does internally: extract the FM block
    fm_block = serialized.split("---", 2)[1].strip()

    md = monitor._parse_yaml_frontmatter(fm_block)
    assert md["id"] == "task_round_001"
    assert md["output_path"] == meta["output_path"]
    assert md["context_files"] == meta["context_files"]
    assert md["retry_count"] == 3
    assert md["parent_task_id"] == "task_parent_999"
    assert md["chain_to"] == "qa"


# ---------------------------------------------------------------------------
# get_pending_approval_tasks — exercises the parser end-to-end
# ---------------------------------------------------------------------------


def _write_pending_approval_task(folder: Path, task_id: str, **overrides):
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": task_id,
        "type": "code",
        "priority": "medium",
        "created_by": "orchestrator",
        "created_at": "2026-05-17T10:00:00",
        "assigned_to": "claude-code",
        "status": "pending_approval",
        "output_path": rf"C:\Users\X\outbox\{task_id}.md",
        "context_files": [r"C:\Users\X\ctx.md"],
    }
    meta.update(overrides)
    post = frontmatter.Post(f"## Task\n\nDo {task_id}\n", **meta)
    (folder / f"{task_id}.task.md").write_text(
        frontmatter.dumps(post), encoding="utf-8"
    )


def test_get_pending_approval_tasks_returns_correct_metadata(monitor, fake_project):
    pending_dir = fake_project / "agents" / "claude-code" / "pending"
    _write_pending_approval_task(pending_dir, "task_pa_001")

    tasks = monitor.get_pending_approvals()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task_pa_001"
    assert tasks[0]["type"] == "code"
    assert tasks[0]["assigned_to"] == "pending_approval"
    assert tasks[0]["status"] == "pending_approval"


def test_get_pending_approval_tasks_handles_windows_paths(monitor, fake_project):
    """The original M5 motivation: a Windows path in output_path used to
    break yaml.safe_load (per stale CLAUDE.md note). It doesn't anymore."""
    pending_dir = fake_project / "agents" / "claude-code" / "pending"
    _write_pending_approval_task(
        pending_dir,
        "task_pa_002",
        output_path=r"C:\Users\JAAS\Desktop\AI Team\outbox\result.md",
    )

    tasks = monitor.get_pending_approvals()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task_pa_002"


def test_get_pending_approval_tasks_empty_when_dir_missing(monitor, fake_project):
    """Removing the pending dir entirely shouldn't crash the dashboard."""
    import shutil
    shutil.rmtree(fake_project / "agents" / "claude-code" / "pending")
    assert monitor.get_pending_approvals() == []


# ---------------------------------------------------------------------------
# approve_task / reject_task — verify they preserve frontmatter fields
# ---------------------------------------------------------------------------


def test_approve_task_moves_file_and_updates_status(monitor, fake_project):
    pending_dir = fake_project / "agents" / "claude-code" / "pending"
    inbox_dir = fake_project / "agents" / "claude-code" / "inbox"
    _write_pending_approval_task(pending_dir, "task_app_001")

    assert monitor.approve_task("task_app_001") is True

    # Source file gone
    assert not (pending_dir / "task_app_001.task.md").exists()
    # Destination file present
    moved = inbox_dir / "task_app_001.task.md"
    assert moved.exists()
    # Status flipped to "pending"
    parsed = frontmatter.loads(moved.read_text(encoding="utf-8"))
    assert parsed["status"] == "pending"


def test_approve_task_preserves_all_other_fields(monitor, fake_project):
    """Regression for the N2-style bug: approve must not drop fields."""
    pending_dir = fake_project / "agents" / "claude-code" / "pending"
    inbox_dir = fake_project / "agents" / "claude-code" / "inbox"
    _write_pending_approval_task(
        pending_dir,
        "task_app_002",
        parent_task_id="task_parent_x",
        retry_count=2,
        chain_to="qa",
    )

    assert monitor.approve_task("task_app_002") is True

    parsed = frontmatter.loads(
        (inbox_dir / "task_app_002.task.md").read_text(encoding="utf-8")
    )
    assert parsed["parent_task_id"] == "task_parent_x"
    assert parsed["retry_count"] == 2
    assert parsed["chain_to"] == "qa"
    assert parsed["output_path"]  # still present


def test_approve_task_returns_false_when_missing(monitor):
    assert monitor.approve_task("does_not_exist") is False


def test_reject_task_appends_reason_and_moves_to_failed(monitor, fake_project):
    pending_dir = fake_project / "agents" / "claude-code" / "pending"
    failed_dir = fake_project / "failed"
    _write_pending_approval_task(pending_dir, "task_rej_001")

    assert monitor.reject_task("task_rej_001", "out of scope") is True

    assert not (pending_dir / "task_rej_001.task.md").exists()
    rejected = failed_dir / "task_rej_001.task.md"
    assert rejected.exists()
    text = rejected.read_text(encoding="utf-8")
    assert "status: rejected" in text
    assert "## Rejection" in text
    assert "out of scope" in text


def test_reject_task_returns_false_when_missing(monitor):
    assert monitor.reject_task("does_not_exist", "reason") is False
