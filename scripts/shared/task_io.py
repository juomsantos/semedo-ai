"""
task_io.py — Task file read/write/move helpers.
"""

import os
import shutil
import glob
from datetime import datetime
from pathlib import Path

import frontmatter  # python-frontmatter

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_folder(name: str) -> Path:
    return PROJECT_ROOT / name


def list_pending_tasks(inbox_path):
    inbox = Path(inbox_path)
    return sorted(inbox.glob("*.task.md"))


def read_task(task_path):
    path = Path(task_path)
    post = frontmatter.load(str(path))
    return {"meta": dict(post.metadata), "body": post.content, "path": path}


def write_result(output_path, content, meta=None):
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if meta:
        post = frontmatter.Post(content, **meta)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")
    return path


def move_task(task_path, destination_folder):
    src = Path(task_path)
    dst_folder = Path(destination_folder)
    dst_folder.mkdir(parents=True, exist_ok=True)
    dst = dst_folder / src.name
    shutil.move(str(src), str(dst))
    return dst


def mark_processing(task_path):
    return move_task(task_path, get_folder("processing"))


def mark_completed(task_path):
    return move_task(task_path, get_folder("outbox"))


def mark_failed(task_path):
    return move_task(task_path, get_folder("failed"))


def generate_task_id():
    now = datetime.utcnow()
    return f"task_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond:06d}"


def create_task_file(
    inbox_path,
    task_type,
    description,
    expected_output,
    assigned_to="orchestrator",
    priority="medium",
    created_by="claude-cowork",
    context_files=None,
    chain_to=None,
    retry_count=0,
    original_description=None,
):
    task_id = generate_task_id()
    output_path = str(get_folder("outbox") / f"{task_id}_result.md")

    meta = {
        "id": task_id,
        "type": task_type,
        "priority": priority,
        "created_by": created_by,
        "created_at": datetime.utcnow().isoformat(),
        "assigned_to": assigned_to,
        "status": "pending",
        "output_path": output_path,
        "context_files": context_files or [],
    }

    if chain_to is not None:
        meta["chain_to"] = chain_to
    if retry_count > 0:
        meta["retry_count"] = retry_count
    if original_description is not None:
        meta["original_description"] = original_description

    body = f"## Task Description\n\n{description}\n\n## Expected Output\n\n{expected_output}"
    post = frontmatter.Post(body, **meta)

    inbox = Path(inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    task_path = inbox / f"{task_id}.task.md"
    task_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    return task_path
