"""
file_watcher.py — File system event watching for task-driven agent triggering.

Monitors inbox and worker folders for .task.md file creation events.
Coalesces rapid file creations to avoid spawning duplicate agent processes.

Usage:
    from shared.file_watcher import TaskWatcher

    watcher = TaskWatcher()
    watcher.watch_folder(
        folder_path=Path("inbox"),
        callback=lambda: print("Task detected!"),
        agent_name="orchestrator"
    )
    watcher.start()
    # ... do other work ...
    watcher.stop()
"""

import time
from pathlib import Path
from threading import Timer, Lock
from typing import Callable, Optional
from collections import defaultdict

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class TaskWatcher:
    """
    Monitors multiple task folders and triggers callbacks on task creation.
    Coalesces rapid file creations within a window to avoid duplicate triggers.
    """

    def __init__(self, coalescence_window: float = 0.5):
        """
        Args:
            coalescence_window: Seconds to wait after first detection before triggering
                               (allows batching of rapid file creations).
        """
        self.coalescence_window = coalescence_window
        self.observer = Observer()
        self._callbacks = {}  # folder_path -> (callback, agent_name)
        self._pending_timers = {}  # folder_path -> Timer
        self._timer_lock = Lock()

    def watch_folder(self, folder_path: Path, callback: Callable[[], None], agent_name: str):
        """
        Register a folder to watch.

        Args:
            folder_path:  Path object of the folder to monitor.
            callback:     Function to call when a .task.md file is created.
            agent_name:   Name of the agent (for logging).
        """
        folder_path = Path(folder_path).resolve()
        self._callbacks[folder_path] = (callback, agent_name)

    def start(self):
        """Start watching all registered folders and scan for existing files."""
        for folder_path in self._callbacks.keys():
            if folder_path.exists():
                handler = _TaskCreatedHandler(
                    folder_path=folder_path,
                    on_task_created=self._on_task_created,
                )
                self.observer.schedule(handler, str(folder_path), recursive=False)

                # Scan for any existing .task.md files and trigger callback
                # This ensures pre-existing tasks are picked up when watcher starts
                for task_file in folder_path.glob("*.task.md"):
                    self._on_task_created(folder_path)
                    break  # Only trigger once per folder (coalescing)

        self.observer.start()

    def stop(self):
        """Stop watching and clean up."""
        self.observer.stop()
        self.observer.join(timeout=5)

        # Cancel any pending timers
        with self._timer_lock:
            for timer in self._pending_timers.values():
                timer.cancel()
            self._pending_timers.clear()

    def _on_task_created(self, folder_path: Path):
        """Called when a .task.md file is created in a monitored folder."""
        with self._timer_lock:
            # Cancel any existing timer for this folder
            if folder_path in self._pending_timers:
                self._pending_timers[folder_path].cancel()

            # Schedule a new trigger after the coalescence window
            callback, _ = self._callbacks[folder_path]
            timer = Timer(self.coalescence_window, callback)
            timer.daemon = True
            self._pending_timers[folder_path] = timer
            timer.start()


class _TaskCreatedHandler(FileSystemEventHandler):
    """Internal handler for watchdog events."""

    def __init__(self, folder_path: Path, on_task_created: Callable[[Path], None]):
        self.folder_path = folder_path
        self.on_task_created = on_task_created

    def on_created(self, event):
        """Triggered when a file is created."""
        if not event.is_directory and event.src_path.endswith(".task.md"):
            self.on_task_created(self.folder_path)

    def on_modified(self, event):
        """Triggered when a file is modified."""
        if not event.is_directory and event.src_path.endswith(".task.md"):
            self.on_task_created(self.folder_path)

    def on_moved(self, event):
        """Triggered when a file is renamed/moved into the folder.

        Atomic task-file writes (shared.task_io._atomic_write_text) create a
        temp file and then os.replace() it into place. On Windows that rename
        surfaces as a FileMovedEvent whose dest_path is the final .task.md —
        never an on_created/on_modified for the .task.md itself. Without this
        handler, atomically-written tasks land on disk but never trigger the
        agent, stalling the whole pipeline.
        """
        if not event.is_directory and str(event.dest_path).endswith(".task.md"):
            self.on_task_created(self.folder_path)
