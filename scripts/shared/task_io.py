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
    with open(str(path), 'r', encoding='utf-8') as f:
        post = frontmatter.load(f)
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
    new_path = move_task(task_path, get_folder("processing"))
    # Update status to "processing" so recover_orphaned_tasks doesn't re-dispatch it.
    # Use string-based replacement to preserve ALL original frontmatter fields.
    # Avoids python-frontmatter round-trip which can silently drop fields on
    # Windows paths / datetime values (the N2 bug).
    import re as _re
    content = new_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            new_fm = _re.sub(r"^status:.*", "status: processing", parts[1], flags=_re.MULTILINE)
            if "status:" not in new_fm:
                new_fm = new_fm.rstrip("\n") + "\nstatus: processing\n"
            content = f"---{new_fm}---{parts[2]}"
            new_path.write_text(content, encoding="utf-8")
    return new_path


def mark_awaiting_validation(task_path):
    """Move task to validation folder and update status to awaiting_validation."""
    import re as _re
    new_path = move_task(task_path, get_folder("validation"))
    content = new_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            new_fm = _re.sub(r"^status:.*", "status: awaiting_validation", parts[1], flags=_re.MULTILINE)
            if "status:" not in new_fm:
                new_fm = new_fm.rstrip("\n") + "\nstatus: awaiting_validation\n"
            content = f"---{new_fm}---{parts[2]}"
            new_path.write_text(content, encoding="utf-8")
    return new_path


def mark_completed(task_path):
    new_path = move_task(task_path, get_folder("outbox"))
    task = read_task(new_path)
    task["meta"]["status"] = "complete"
    return write_result(str(new_path), task["body"], meta=task["meta"])


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
    parent_task_id=None,
    depends_on=None,
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
    if parent_task_id is not None:
        meta["parent_task_id"] = parent_task_id
    if depends_on is not None:
        meta["depends_on"] = depends_on

    body = f"## Task Description\n\n{description}\n\n## Expected Output\n\n{expected_output}"
    post = frontmatter.Post(body, **meta)

    inbox = Path(inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    task_path = inbox / f"{task_id}.task.md"
    task_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    return task_path


def list_validation_tasks(validation_path=None):
    """List all tasks awaiting orchestrator validation."""
    if validation_path is None:
        validation_path = get_folder("validation")
    validation = Path(validation_path)
    return sorted(validation.glob("*.task.md")) if validation.exists() else []


def get_completed_subtasks_by_parent(validation_path=None):
    """
    Group completed subtasks by parent_task_id.
    Returns: dict of {parent_task_id: [task1, task2, ...]}
    """
    validation_tasks = list_validation_tasks(validation_path)
    grouped = {}

    for task_path in validation_tasks:
        task = read_task(task_path)
        parent_id = task["meta"].get("parent_task_id")
        if parent_id:
            if parent_id not in grouped:
                grouped[parent_id] = []
            grouped[parent_id].append(task)

    return grouped


def resolve_task_dependencies(inboxes_dict: dict) -> None:
    """
    Scan all agent inboxes for tasks with unresolved dependencies.
    If a dependency is completed (in outbox), add its output to context_files
    and remove the depends_on field.
    """
    outbox = get_folder("outbox")

    for agent_name, inbox_path in inboxes_dict.items():
        pending_tasks = list_pending_tasks(inbox_path)

        for task_path in pending_tasks:
            task = read_task(task_path)
            depends_on = task["meta"].get("depends_on", [])

            if not depends_on:
                continue

            all_resolved = True
            resolved_outputs = []

            for dep_task_id in depends_on:
                # Read completed task metadata to get its actual output_path
                dep_task_path = outbox / f"{dep_task_id}.task.md"
                if dep_task_path.exists():
                    dep_task = read_task(dep_task_path)
                    output_path = dep_task["meta"].get("output_path")
                    if output_path and Path(output_path).exists():
                        resolved_outputs.append(output_path)
                    else:
                        all_resolved = False
                        break
                else:
                    # Fallback: check for result file with standard naming
                    dep_output_path = outbox / f"{dep_task_id}_result.md"
                    if dep_output_path.exists():
                        resolved_outputs.append(str(dep_output_path))
                    else:
                        all_resolved = False
                        break

            if all_resolved:
                task["meta"]["context_files"] = list(set(
                    task["meta"].get("context_files", []) + resolved_outputs
                ))
                del task["meta"]["depends_on"]
                body = task["body"]
                write_result(str(task_path), body, meta=task["meta"])
