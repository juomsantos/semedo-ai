"""
test_file_watcher.py — Regression guard for the atomic-write / file-watcher contract.

Background (the regression this file locks out):
    All task-file writes route through ``shared.task_io._atomic_write_text``,
    which writes a ``*.tmp`` file and then ``os.replace()``s it into place.
    On the destination ``.task.md`` that rename surfaces as a watchdog
    ``FileMovedEvent`` (``on_moved``) — NOT ``on_created`` / ``on_modified``.
    ``_TaskCreatedHandler`` originally handled only created/modified events, so
    atomically-written tasks landed on disk but never triggered their agent and
    the whole pipeline stalled. The handler now also implements ``on_moved``.

Two layers of coverage:
    * Handler-level (deterministic, no real I/O): feed synthetic watchdog events
      straight into ``_TaskCreatedHandler`` and assert exactly when the callback
      fires. These are the primary regression lock — fast and non-flaky.
    * End-to-end (real Observer + the real ``_atomic_write_text``): proves the
      actual atomic-write mechanism is observable by a live watcher.
"""

from __future__ import annotations

import threading
from pathlib import Path

from shared.file_watcher import TaskWatcher, _TaskCreatedHandler

from watchdog.events import (
    DirCreatedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)


# ---------------------------------------------------------------------------
# Handler-level tests — deterministic, drive _TaskCreatedHandler directly.
# ---------------------------------------------------------------------------

def _make_handler():
    """Return (handler, calls) where ``calls`` accumulates folder_path triggers."""
    calls: list[Path] = []
    folder = Path("inbox")
    handler = _TaskCreatedHandler(
        folder_path=folder,
        on_task_created=lambda fp: calls.append(fp),
    )
    return handler, calls, folder


def test_on_moved_task_file_triggers():
    """The regression: an os.replace rename into the folder must trigger.

    A FileMovedEvent whose dest_path ends in .task.md is exactly what
    _atomic_write_text produces. Before the fix this was silently dropped.
    """
    handler, calls, folder = _make_handler()
    handler.on_moved(
        FileMovedEvent("inbox/task_x.task.md.1234.tmp", "inbox/task_x.task.md")
    )
    assert calls == [folder]


def test_on_created_tmp_file_is_ignored():
    """The temp file created before the rename must NOT trigger.

    _atomic_write_text first creates ``<name>.<pid>.tmp``; that on_created
    event ends in .tmp, not .task.md, so it should be filtered out — otherwise
    every atomic write would fire twice.
    """
    handler, calls, _ = _make_handler()
    handler.on_created(FileCreatedEvent("inbox/task_x.task.md.1234.tmp"))
    assert calls == []


def test_on_created_task_file_triggers():
    """A plain in-place create (non-atomic writer) still triggers."""
    handler, calls, folder = _make_handler()
    handler.on_created(FileCreatedEvent("inbox/task_x.task.md"))
    assert calls == [folder]


def test_on_modified_task_file_triggers():
    """An in-place modification of a .task.md still triggers."""
    handler, calls, folder = _make_handler()
    handler.on_modified(FileModifiedEvent("inbox/task_x.task.md"))
    assert calls == [folder]


def test_on_moved_non_task_file_is_ignored():
    """A rename whose destination isn't a .task.md must not trigger."""
    handler, calls, _ = _make_handler()
    handler.on_moved(FileMovedEvent("inbox/foo.tmp", "inbox/foo.md"))
    assert calls == []


def test_directory_events_are_ignored():
    """Directory create/move events must never trigger the callback."""
    handler, calls, _ = _make_handler()
    handler.on_created(DirCreatedEvent("inbox/subdir"))
    handler.on_moved(DirMovedEvent("inbox/a", "inbox/b.task.md"))
    assert calls == []


# ---------------------------------------------------------------------------
# End-to-end test — real Observer + the real atomic-write helper.
# ---------------------------------------------------------------------------

def test_atomic_write_is_detected_by_live_watcher(tmp_path):
    """Drive a real TaskWatcher and write a task via the real _atomic_write_text.

    This is the faithful end-to-end version: it imports the actual atomic-write
    function used by every task writer, so if someone ever changes the write
    mechanism to something the watcher can't observe, this test fails.
    """
    from shared.task_io import _atomic_write_text

    inbox = tmp_path / "inbox"
    inbox.mkdir()

    fired = threading.Event()
    watcher = TaskWatcher(coalescence_window=0.05)
    watcher.watch_folder(
        folder_path=inbox,
        callback=fired.set,
        agent_name="orchestrator",
    )
    watcher.start()
    try:
        # Exactly how a task lands in a real inbox: temp file + os.replace.
        _atomic_write_text(
            inbox / "task_20260617_101500_000000.task.md",
            "---\nid: task_20260617_101500_000000\n---\n\n## Task\nhello\n",
        )
        assert fired.wait(timeout=10), (
            "live watcher did not fire on an atomic (os.replace) task write — "
            "the on_moved regression has returned"
        )
    finally:
        watcher.stop()


def test_startup_scan_picks_up_preexisting_task(tmp_path):
    """A task already present when the watcher starts is picked up by the boot scan.

    This is the path that kept the pipeline limping during the regression
    (restarting the scheduler drained the inbox even though live events were
    being missed), so it's worth guarding too.
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "task_preexisting.task.md").write_text("---\nid: x\n---\n", encoding="utf-8")

    fired = threading.Event()
    watcher = TaskWatcher(coalescence_window=0.05)
    watcher.watch_folder(
        folder_path=inbox,
        callback=fired.set,
        agent_name="orchestrator",
    )
    watcher.start()
    try:
        assert fired.wait(timeout=10), "startup scan failed to pick up a pre-existing task"
    finally:
        watcher.stop()
