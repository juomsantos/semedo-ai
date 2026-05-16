"""
chat_context.py — Build context snapshots for the chat LLM.
"""

import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any


def _parse_frontmatter(content: str) -> Dict[str, str]:
    """Parse YAML frontmatter from task/result files (same logic as task_monitor)."""
    lines = content.split('\n')
    parsed = {}
    in_frontmatter = False
    frontmatter_end = -1

    for i, line in enumerate(lines):
        if i == 0 and line.strip() == '---':
            in_frontmatter = True
            continue
        if in_frontmatter and line.strip() == '---':
            frontmatter_end = i
            break
        if in_frontmatter:
            if ':' in line:
                key, val = line.split(':', 1)
                parsed[key.strip()] = val.strip()

    return parsed


def _get_task_body_preview(content: str, max_chars: int = 300) -> str:
    """Extract task body (after frontmatter) and limit to max_chars."""
    lines = content.split('\n')
    start = 0
    for i, line in enumerate(lines):
        if i > 0 and line.strip() == '---':
            start = i + 1
            break
    body = '\n'.join(lines[start:]).strip()
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n... [{len(body) - max_chars} more chars]"
    return body


def build_base_snapshot(project_root: Path) -> str:
    """Build a plain-text block with pipeline state for injection into system prompt."""
    project_root = Path(project_root)

    # Count tasks per folder
    inbox_count = len(list((project_root / "inbox").glob("*.task.md"))) if (project_root / "inbox").exists() else 0
    processing_count = len(list((project_root / "processing").glob("*.task.md"))) if (project_root / "processing").exists() else 0
    validation_count = len(list((project_root / "validation").glob("*.task.md"))) if (project_root / "validation").exists() else 0
    outbox_count = len(list((project_root / "outbox").glob("*_result.md"))) if (project_root / "outbox").exists() else 0
    failed_count = len(list((project_root / "failed").glob("*.task.md"))) if (project_root / "failed").exists() else 0
    pending_approval_count = len(list((project_root / "agents" / "claude-code" / "pending").glob("*.task.md"))) if (project_root / "agents" / "claude-code" / "pending").exists() else 0

    snapshot = f"""## Pipeline Status

Task counts:
- Inbox (pending): {inbox_count}
- Processing: {processing_count}
- Validation: {validation_count}
- Completed: {outbox_count}
- Failed: {failed_count}
- Awaiting Approval (claude-code): {pending_approval_count}

## Active Tasks

"""

    # Gather all active tasks
    active_folders = [
        (project_root / "inbox", "inbox"),
        (project_root / "processing", "processing"),
        (project_root / "validation", "validation"),
        (project_root / "agents" / "orchestrator" / "inbox", "orchestrator"),
        (project_root / "agents" / "coder" / "inbox", "coder"),
        (project_root / "agents" / "research" / "inbox", "research"),
        (project_root / "agents" / "qa" / "inbox", "qa"),
        (project_root / "agents" / "claude-code" / "inbox", "claude-code"),
        (project_root / "agents" / "claude-code" / "pending", "claude-code-pending"),
    ]

    active_tasks = []
    for folder, source in active_folders:
        if folder.exists():
            for task_file in folder.glob("*.task.md"):
                try:
                    content = task_file.read_text(encoding='utf-8')
                    meta = _parse_frontmatter(content)
                    task_id = meta.get('id', task_file.stem.replace('.task', ''))
                    task_type = meta.get('type', 'unknown')
                    status = meta.get('status', 'unknown')
                    assigned_to = meta.get('assigned_to', source)
                    created_at_str = meta.get('created_at', '')
                    body_preview = _get_task_body_preview(content, 300)
                    depends_on = meta.get('depends_on', '')

                    # Calculate age
                    try:
                        created_at = datetime.fromisoformat(created_at_str)
                        age = datetime.now(timezone.utc) - created_at
                        age_str = f"{int(age.total_seconds() / 60)}m"
                    except:
                        age_str = "?"

                    depends_str = f"\n  depends_on: {depends_on}" if depends_on else ""
                    active_tasks.append(f"- {task_id} ({task_type}, {status}, {assigned_to}, {age_str}){depends_str}\n  {body_preview[:100]}...")
                except:
                    pass

    if active_tasks:
        snapshot += "\n".join(active_tasks)
    else:
        snapshot += "No active tasks."

    # Last 14 days of completed parent tasks
    snapshot += "\n\n## Recently Completed Tasks (last 14 days)\n\n"

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=14)
    completed_tasks = []

    outbox = project_root / "outbox"
    if outbox.exists():
        for result_file in sorted(outbox.glob("*_result.md"), reverse=True):
            try:
                task_file = outbox / result_file.name.replace("_result.md", ".task.md")
                if task_file.exists():
                    content = task_file.read_text(encoding='utf-8')
                    meta = _parse_frontmatter(content)
                    # Only include parent tasks (no parent_task_id)
                    if not meta.get('parent_task_id'):
                        created_at_str = meta.get('created_at', '')
                        try:
                            created_at = datetime.fromisoformat(created_at_str)
                            if created_at < cutoff:
                                break
                        except:
                            pass

                        task_id = meta.get('id', result_file.stem.replace('_result', ''))
                        task_type = meta.get('type', 'unknown')
                        result_content = result_file.read_text(encoding='utf-8')
                        result_preview = result_content[:1000]
                        if len(result_content) > 1000:
                            result_preview += f"\n... [{len(result_content) - 1000} more chars]"

                        completed_tasks.append(f"- {task_id} ({task_type})\n  {result_preview}")
            except:
                pass

    if completed_tasks:
        snapshot += "\n".join(completed_tasks[:30])  # Cap at 30k total
    else:
        snapshot += "No completed tasks in the last 14 days."

    return snapshot


def extract_task_id(message: str) -> Optional[str]:
    """Extract a task ID (task_YYYYMMDD_HHMMSS_microseconds) from message."""
    match = re.search(r'task_\d{8}_\d{6}_\d+', message)
    return match.group(0) if match else None


def get_deep_task_context(task_id: str, project_root: Path) -> str:
    """Return detailed context for a specific task."""
    project_root = Path(project_root)
    context = f"\n## Deep Task Context: {task_id}\n\n"

    # Search for the task file in all folders
    task_file = None
    for folder in [
        project_root / "inbox",
        project_root / "processing",
        project_root / "validation",
        project_root / "agents" / "orchestrator" / "inbox",
        project_root / "agents" / "coder" / "inbox",
        project_root / "agents" / "research" / "inbox",
        project_root / "agents" / "qa" / "inbox",
        project_root / "agents" / "claude-code" / "inbox",
        project_root / "agents" / "claude-code" / "pending",
    ]:
        candidate = folder / f"{task_id}.task.md"
        if candidate.exists():
            task_file = candidate
            break

    if task_file:
        try:
            content = task_file.read_text(encoding='utf-8')
            meta = _parse_frontmatter(content)
            body = _get_task_body_preview(content, 3000)
            context += f"**Task Body:** ({len(body)} chars)\n{body}\n\n"
            context += f"**Metadata:**\n"
            for key in ['status', 'type', 'priority', 'assigned_to', 'depends_on', 'output_path']:
                if key in meta:
                    context += f"- {key}: {meta[key]}\n"
        except:
            context += "Could not read task file.\n"
    else:
        context += "Task file not found in active folders.\n"

    # Try to find result file
    result_file = project_root / "outbox" / f"{task_id}_result.md"
    if result_file.exists():
        try:
            result_content = result_file.read_text(encoding='utf-8')
            result_preview = result_content[:4000]
            if len(result_content) > 4000:
                result_preview += f"\n... [{len(result_content) - 4000} more chars]"
            context += f"\n**Result:** ({len(result_content)} chars total)\n{result_preview}\n"
        except:
            pass

    # Get last 30 log lines mentioning this task_id
    logs_dir = project_root / "logs"
    log_matches = []
    if logs_dir.exists():
        for log_file in logs_dir.rglob("general.log"):
            try:
                lines = log_file.read_text(encoding='utf-8').split('\n')
                for line in lines:
                    if task_id in line:
                        log_matches.append(line)
            except:
                pass

    if log_matches:
        context += f"\n**Recent Logs (last 30 mentions):**\n"
        for line in log_matches[-30:]:
            context += f"- {line}\n"

    return context
