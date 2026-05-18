"""
task_io.py — Task file read/write/move helpers.
"""

import os
import re
import shutil
import glob
from datetime import datetime
from pathlib import Path

import frontmatter  # python-frontmatter

from shared.validation_context import prepend_validation_context

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


def safe_read_context(cf, logger=None):
    """
    Safely read a context-file path supplied via a task's ``context_files`` field.

    Returns the file contents as a string, or ``None`` if the path:
      - is empty / unreadable;
      - resolves to a location outside ``PROJECT_ROOT`` (path-traversal guard);
      - does not exist.

    Rejections are logged via ``logger`` when supplied so unexpected paths show up
    in the agent log instead of being silently skipped.

    This is the only sanctioned way for agents to materialize a ``context_files``
    entry — task frontmatter is LLM- or user-supplied and must not be trusted.
    """
    if not cf:
        return None
    try:
        cf_path = Path(cf).resolve()
    except (OSError, ValueError) as e:
        if logger:
            logger.warning(f"Rejecting context file (invalid path): {cf!r} ({type(e).__name__}: {e})")
        return None

    project_root_resolved = PROJECT_ROOT.resolve()
    try:
        cf_path.relative_to(project_root_resolved)
    except ValueError:
        if logger:
            logger.warning(
                f"Rejecting context file outside project root: {cf!r} -> {cf_path}"
            )
        return None

    if not cf_path.exists():
        if logger:
            logger.warning(f"Context file not found, skipping: {cf}")
        return None
    if not cf_path.is_file():
        if logger:
            logger.warning(f"Context file is not a regular file, skipping: {cf}")
        return None

    try:
        return cf_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as e:
        if logger:
            logger.warning(f"Could not read context file {cf}: {type(e).__name__}: {e}")
        return None


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
    content = new_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            new_fm = re.sub(r"^status:.*", "status: processing", parts[1], flags=re.MULTILINE)
            if "status:" not in new_fm:
                new_fm = new_fm.rstrip("\n") + "\nstatus: processing\n"
            content = f"---{new_fm}---{parts[2]}"
            new_path.write_text(content, encoding="utf-8")
    return new_path


def mark_awaiting_validation(task_path):
    """Move task to validation folder and update status to awaiting_validation."""
    new_path = move_task(task_path, get_folder("validation"))
    content = new_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            new_fm = re.sub(r"^status:.*", "status: awaiting_validation", parts[1], flags=re.MULTILINE)
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
    validation_context=None,
):
    """
    validation_context: optional dict with keys:
      - decision_type: "redo" | "refine" | "additional_work"
      - reasoning: the orchestrator's explanation for why this follow-up was created
    When provided, the decision type is written to frontmatter AND injected as a
    ## Validation Context section at the top of the task body so workers see it clearly.
    """
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
    if validation_context is not None:
        # Store the full dict so downstream agents (e.g. coder → QA) can forward it
        meta["validation_context"] = validation_context

    # The Validation Context block (when present) is the primary signal a
    # follow-up worker should act on — it appears before the task description
    # so it cannot be missed even if the LLM skims the frontmatter. The
    # wording lives in shared/validation_context.py so QA can inject the
    # identical block at LLM-call-time (see agent_qa.review_with_llm).
    description_section = f"## Task Description\n\n{description}\n\n## Expected Output\n\n{expected_output}"
    body = prepend_validation_context(description_section, validation_context)
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


def read_subtask_result(output_path: str) -> str:
    """Read full subtask result file content. Return content or error message if missing."""
    try:
        return Path(output_path).read_text(encoding='utf-8')
    except FileNotFoundError:
        return f"[Result file not found: {output_path}]"
    except Exception as e:
        return f"[Error reading result file {output_path}: {e}]"


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
