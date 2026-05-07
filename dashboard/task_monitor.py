"""
task_monitor.py — File system scanner for real-time task monitoring.

Polls the project folders (inbox/, processing/, outbox/, failed/, agents/*/inbox/)
to build a complete picture of system state and task progress.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import re


class TaskMonitor:
    """Scan file system for task status and metrics."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.inbox = self.project_root / "inbox"
        self.processing = self.project_root / "processing"
        self.outbox = self.project_root / "outbox"
        self.failed = self.project_root / "failed"
        self.logs_dir = self.project_root / "logs"
        self.agents_dir = self.project_root / "agents"
        self.claude_code_pending = self.agents_dir / "claude-code" / "pending"

    def get_system_status(self) -> Dict[str, Any]:
        """Get overall system status and metrics."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "pending": self._count_pending_tasks(),
                "processing": self._count_processing_tasks(),
                "completed": self._count_completed_tasks(),
                "failed": self._count_failed_tasks(),
                "awaiting_approval": self._count_pending_approvals(),
            },
            "ollama_lock": self._check_ollama_lock(),
            "agent_stats": self._get_agent_stats(),
        }

    def _count_pending_approvals(self) -> int:
        """Count tasks awaiting approval in the pending folder."""
        if not self.claude_code_pending.exists():
            return 0
        return len([f for f in self.claude_code_pending.glob("*.task.md")])

    def _count_pending_tasks(self) -> int:
        """Count pending tasks in inbox and agent inboxes."""
        count = 0
        if self.inbox.exists():
            count += len(list(self.inbox.glob("*.task.md")))
        
        # Count in per-agent inboxes
        if self.agents_dir.exists():
            for agent_dir in self.agents_dir.iterdir():
                agent_inbox = agent_dir / "inbox"
                if agent_inbox.exists():
                    count += len(list(agent_inbox.glob("*.task.md")))
        
        return count

    def _count_processing_tasks(self) -> int:
        """Count tasks currently in processing."""
        if not self.processing.exists():
            return 0
        return len([f for f in self.processing.glob("*.task.md")])

    def _count_completed_tasks(self) -> int:
        """Count successfully completed tasks."""
        if not self.outbox.exists():
            return 0
        return len([f for f in self.outbox.glob("*.task.md")])

    def _count_failed_tasks(self) -> int:
        """Count failed tasks."""
        if not self.failed.exists():
            return 0
        return len([f for f in self.failed.glob("*.task.md")])

    def _check_ollama_lock(self) -> Optional[Dict[str, Any]]:
        """Check orchestrator lock file for current PID."""
        lock_file = self.processing / "orchestrator.lock"
        if lock_file.exists():
            try:
                pid = int(lock_file.read_text().strip())
                return {"pid": pid, "timestamp": lock_file.stat().st_mtime}
            except Exception:
                return None
        return None

    def get_all_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all tasks from all locations with status inferred from location."""
        tasks = []
        
        # Pending tasks from inbox
        if self.inbox.exists():
            for task_file in sorted(self.inbox.glob("*.task.md"), reverse=True)[:limit]:
                task = self._parse_task_file(task_file, "pending", "inbox")
                if task:
                    tasks.append(task)
        
        # Pending tasks from agent inboxes
        if self.agents_dir.exists():
            for agent_dir in sorted(self.agents_dir.iterdir()):
                agent_inbox = agent_dir / "inbox"
                if agent_inbox.exists():
                    for task_file in sorted(agent_inbox.glob("*.task.md"), reverse=True)[:limit]:
                        assigned_to = agent_dir.name
                        task = self._parse_task_file(task_file, "pending", f"agents/{assigned_to}/inbox", assigned_to=assigned_to)
                        if task:
                            tasks.append(task)
        
        # Processing tasks
        if self.processing.exists():
            for task_file in sorted(self.processing.glob("*.task.md"), reverse=True)[:limit]:
                task = self._parse_task_file(task_file, "processing", "processing")
                if task:
                    tasks.append(task)
        
        # Completed tasks
        if self.outbox.exists():
            for task_file in sorted(self.outbox.glob("*.task.md"), reverse=True)[:limit]:
                task = self._parse_task_file(task_file, "completed", "outbox")
                if task:
                    tasks.append(task)
        
        # Failed tasks
        if self.failed.exists():
            for task_file in sorted(self.failed.glob("*.task.md"), reverse=True)[:limit]:
                task = self._parse_task_file(task_file, "failed", "failed")
                if task:
                    tasks.append(task)
        
        # Pending approval tasks
        if self.claude_code_pending.exists():
            for task_file in sorted(self.claude_code_pending.glob("*.task.md"), reverse=True)[:limit]:
                task = self._parse_task_file(task_file, "pending_approval", "agents/claude-code/pending", assigned_to="pending_approval")
                if task:
                    tasks.append(task)
        
        # Sort by creation time descending
        return sorted(tasks, key=lambda t: t.get("created_at", ""), reverse=True)[:limit]

    def get_task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get complete details for a specific task including result and logs."""
        # Find the task file
        task_file = None
        status = None
        location = None
        
        # Search in all locations
        for folder, status_val in [
            (self.inbox, "pending"),
            (self.processing, "processing"),
            (self.outbox, "completed"),
            (self.failed, "failed"),
            (self.claude_code_pending, "pending_approval"),
        ]:
            if folder.exists():
                found = folder / f"{task_id}.task.md"
                if found.exists():
                    task_file = found
                    status = status_val
                    location = folder
                    break
        
        # Also search agent inboxes
        if not task_file and self.agents_dir.exists():
            for agent_dir in self.agents_dir.iterdir():
                agent_inbox = agent_dir / "inbox"
                found = agent_inbox / f"{task_id}.task.md"
                if found.exists():
                    task_file = found
                    status = "pending"
                    location = agent_inbox
                    break
        
        if not task_file:
            return None
        
        task = self._parse_task_file(task_file, status, str(location))
        if not task:
            return None
        
        # Get result file if it exists
        result_file = self.outbox / f"{task_id}_result.md"
        if not result_file.exists():
            result_file = self.failed / f"{task_id}_result.md"
        
        if result_file.exists():
            task["result"] = result_file.read_text(encoding="utf-8")
        
        # Get logs
        task["logs"] = self._get_task_logs(task_id)

        return task

    def get_task_payload(self, task_id: str) -> Optional[str]:
        """Get raw task file content."""
        # Search in all locations
        for folder in [
            self.inbox,
            self.processing,
            self.outbox,
            self.failed,
            self.claude_code_pending,
        ]:
            if folder.exists():
                task_file = folder / f"{task_id}.task.md"
                if task_file.exists():
                    return task_file.read_text(encoding="utf-8")

        # Also search agent inboxes
        if self.agents_dir.exists():
            for agent_dir in self.agents_dir.iterdir():
                agent_inbox = agent_dir / "inbox"
                task_file = agent_inbox / f"{task_id}.task.md"
                if task_file.exists():
                    return task_file.read_text(encoding="utf-8")

        return None

    def get_agent_stats(self) -> Dict[str, Any]:
        """Get statistics per agent."""
        return self._get_agent_stats()

    def _get_agent_stats(self) -> Dict[str, Any]:
        """Calculate per-agent statistics including token usage."""
        stats = {}
        agents = ["orchestrator", "coder", "research", "qa", "claude-code"]

        # Get token stats
        token_stats = self.get_token_stats()

        for agent in agents:
            agent_logs = self.logs_dir / agent / "general.log"

            completed_count = 0
            error_count = 0

            if agent_logs.exists():
                try:
                    log_content = agent_logs.read_text(encoding="utf-8", errors="ignore")
                    # Count tasks actually picked up and processed.
                    # All agents emit "[INFO] ... Processing task task_<id>" exactly
                    # once per task, making this a reliable cross-agent signal.
                    # The old pattern "[INFO].*complete" was matching "Dependency
                    # resolution complete" (logged every orchestrator cycle) and
                    # missed QA entirely (which never logs the word "complete").
                    completed_count = len(re.findall(r"\[INFO\].*Processing task task_", log_content))
                    error_count = len(re.findall(r"\[ERROR\]", log_content))
                except Exception:
                    pass

            token_info = token_stats.get(agent, {})
            stats[agent] = {
                "completed": completed_count,
                "errors": error_count,
                "prompt_tokens": token_info.get("prompt_tokens", 0),
                "completion_tokens": token_info.get("completion_tokens", 0),
                "llm_calls": token_info.get("llm_calls", 0),
            }

        return stats

    def get_token_stats(self) -> Dict[str, Any]:
        """
        Read logs/<agent>/tokens.jsonl for each agent.
        Returns: { "orchestrator": {"prompt": N, "completion": N, "calls": N}, ... }
        """
        stats = {}
        agents = ["orchestrator", "coder", "research", "qa", "claude-code"]

        for agent in agents:
            token_log = self.logs_dir / agent / "tokens.jsonl"
            prompt_total = 0
            completion_total = 0
            call_count = 0

            if token_log.exists():
                try:
                    with token_log.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                                prompt_total += entry.get("prompt", 0)
                                completion_total += entry.get("completion", 0)
                                call_count += 1
                            except json.JSONDecodeError:
                                # Skip malformed lines silently
                                pass
                except Exception:
                    pass

            stats[agent] = {
                "prompt_tokens": prompt_total,
                "completion_tokens": completion_total,
                "llm_calls": call_count,
            }

        return stats

    def _parse_task_file(
        self,
        task_file: Path,
        status: str,
        location: str,
        assigned_to: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse a task file and extract metadata."""
        try:
            content = task_file.read_text(encoding="utf-8")
            
            # Split frontmatter from body
            if not content.startswith("---"):
                return None
            
            parts = content.split("---", 2)
            if len(parts) < 3:
                return None
            
            frontmatter = parts[1].strip()
            body = parts[2].strip()
            
            # Parse YAML frontmatter (simple parsing)
            metadata = self._parse_yaml_frontmatter(frontmatter)
            
            task_id = metadata.get("id", task_file.stem)
            
            # Calculate age
            file_mtime = datetime.fromtimestamp(task_file.stat().st_mtime, tz=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - file_mtime).total_seconds()
            
            return {
                "id": task_id,
                "type": metadata.get("type", "unknown"),
                "priority": metadata.get("priority", "medium"),
                "created_by": metadata.get("created_by", "unknown"),
                "created_at": metadata.get("created_at", ""),
                "assigned_to": assigned_to or metadata.get("assigned_to", "unknown"),
                "status": status,
                "location": location,
                "retry_count": int(metadata.get("retry_count", 0)),
                "chain_to": metadata.get("chain_to", None),
                "output_path": metadata.get("output_path", ""),
                "age_seconds": int(age_seconds),
                "body_preview": body[:200],
            }
        except Exception:
            return None

    def _parse_yaml_frontmatter(self, yaml_str: str) -> Dict[str, Any]:
        """Simple YAML parser for frontmatter. Handles Windows paths with backslashes."""
        data = {}
        for line in yaml_str.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if ":" in line:
                try:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip()

                    # Remove surrounding quotes, but preserve backslashes in paths
                    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]

                    # Convert retry_count to int
                    if key == "retry_count":
                        try:
                            value = int(value)
                        except ValueError:
                            pass

                    data[key] = value
                except Exception:
                    # Skip lines that can't be parsed
                    continue

        return data

    def _get_task_logs(self, task_id: str) -> List[Dict[str, str]]:
        """Get all log entries for a specific task."""
        logs = []
        
        if not self.logs_dir.exists():
            return logs
        
        # Search each agent's logs
        for agent_log_dir in self.logs_dir.iterdir():
            if agent_log_dir.is_dir():
                agent_name = agent_log_dir.name
                log_file = agent_log_dir / "general.log"
                
                if log_file.exists():
                    try:
                        content = log_file.read_text(encoding="utf-8", errors="ignore")
                        for line in content.split("\n"):
                            if task_id in line:
                                # Parse log line
                                # Format: [TIMESTAMP] [LEVEL] [AGENT] MESSAGE
                                match = re.match(
                                    r"\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(.*)",
                                    line,
                                )
                                if match:
                                    timestamp, level, agent, message = match.groups()
                                    logs.append({
                                        "timestamp": timestamp,
                                        "level": level,
                                        "agent": agent,
                                        "message": message,
                                    })
                    except Exception:
                        pass
        
        return sorted(logs, key=lambda x: x["timestamp"])

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        """Get all tasks awaiting approval with full body text."""
        tasks = []
        if not self.claude_code_pending.exists():
            return tasks
        
        for task_file in sorted(self.claude_code_pending.glob("*.task.md"), reverse=True):
            try:
                content = task_file.read_text(encoding="utf-8")
                
                # Split frontmatter from body
                if not content.startswith("---"):
                    continue
                
                parts = content.split("---", 2)
                if len(parts) < 3:
                    continue
                
                frontmatter = parts[1].strip()
                body = parts[2].strip()
                
                # Parse YAML frontmatter
                metadata = self._parse_yaml_frontmatter(frontmatter)
                
                task_id = metadata.get("id", task_file.stem)
                file_mtime = datetime.fromtimestamp(task_file.stat().st_mtime, tz=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - file_mtime).total_seconds()
                
                tasks.append({
                    "id": task_id,
                    "type": metadata.get("type", "unknown"),
                    "priority": metadata.get("priority", "medium"),
                    "created_by": metadata.get("created_by", "unknown"),
                    "created_at": metadata.get("created_at", ""),
                    "assigned_to": "pending_approval",
                    "status": "pending_approval",
                    "location": "agents/claude-code/pending",
                    "age_seconds": int(age_seconds),
                    "body": body,
                })
            except Exception:
                continue
        
        return tasks

    def approve_task(self, task_id: str) -> bool:
        """Move task from pending to inbox and update status."""
        pending_file = self.claude_code_pending / f"{task_id}.task.md"
        if not pending_file.exists():
            return False
        
        try:
            content = pending_file.read_text(encoding="utf-8")
            
            # Split frontmatter from body
            if not content.startswith("---"):
                return False
            
            parts = content.split("---", 2)
            if len(parts) < 3:
                return False
            
            frontmatter = parts[1].strip()
            body = parts[2].strip()
            
            # Parse frontmatter
            metadata = self._parse_yaml_frontmatter(frontmatter)
            
            # Update status in frontmatter
            lines = frontmatter.split("\n")
            new_lines = []
            for line in lines:
                if line.startswith("status:"):
                    new_lines.append("status: pending")
                else:
                    new_lines.append(line)
            
            new_frontmatter = "\n".join(new_lines)
            new_content = f"---\n{new_frontmatter}\n---\n{body}"
            
            # Create inbox directory if needed
            inbox = self.agents_dir / "claude-code" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            
            # Write to inbox
            inbox_file = inbox / f"{task_id}.task.md"
            inbox_file.write_text(new_content, encoding="utf-8")
            
            # Remove from pending
            pending_file.unlink()
            
            return True
        except Exception:
            return False

    def reject_task(self, task_id: str, reason: str) -> bool:
        """Move task to failed with rejection reason appended."""
        pending_file = self.claude_code_pending / f"{task_id}.task.md"
        if not pending_file.exists():
            return False

        try:
            content = pending_file.read_text(encoding="utf-8")

            # Split frontmatter from body
            if not content.startswith("---"):
                return False

            parts = content.split("---", 2)
            if len(parts) < 3:
                return False

            frontmatter = parts[1].strip()
            body = parts[2].strip()

            # Parse frontmatter
            metadata = self._parse_yaml_frontmatter(frontmatter)

            # Update status in frontmatter
            lines = frontmatter.split("\n")
            new_lines = []
            for line in lines:
                if line.startswith("status:"):
                    new_lines.append("status: rejected")
                else:
                    new_lines.append(line)

            new_frontmatter = "\n".join(new_lines)

            # Append rejection block
            rejection_block = f"\n\n## Rejection\n{reason}"
            new_body = f"{body}{rejection_block}"

            new_content = f"{new_frontmatter}\n---\n{new_body}"

            # Create failed directory if needed
            failed = self.failed
            failed.mkdir(parents=True, exist_ok=True)

            # Write to failed
            failed_file = failed / f"{task_id}.task.md"
            failed_file.write_text(new_content, encoding="utf-8")

            # Remove from pending
            pending_file.unlink()

            return True
        except Exception:
            return False

    def get_agent_logs(self, agent: str, lines: int = 50) -> List[str]:
        """Get recent log lines for a specific agent."""
        log_file = self.logs_dir / agent / "general.log"

        if not log_file.exists():
            return []

        try:
            content = log_file.read_text(encoding="utf-8", errors="ignore")
            all_lines = content.strip().split("\n")
            # Return the last N lines
            return all_lines[-lines:] if len(all_lines) > lines else all_lines
        except Exception:
            return []

    def get_results_by_agent(self, agent: str = "orchestrator") -> Dict[str, Any]:
        """
        Get completed and failed task results grouped by agent.
        Returns: {
            "agent": "orchestrator",
            "completed": [{"id": "...", "type": "...", "output": "....", "created_at": "..."}, ...],
            "failed": [...]
        }
        """
        completed_tasks = []
        failed_tasks = []

        # For orchestrator: scan outbox for .task.md files
        if agent == "orchestrator":
            if self.outbox.exists():
                for task_file in sorted(self.outbox.glob("*.task.md"), reverse=True):
                    task = self._parse_task_file(task_file, "completed", "outbox")
                    if task and task.get("assigned_to") == "orchestrator":
                        # Try to read the result file
                        task_id = task["id"]
                        result_file = self.outbox / f"{task_id}_result.md"
                        output = ""
                        if result_file.exists():
                            try:
                                output = result_file.read_text(encoding="utf-8")[:2000]  # First 2000 chars
                            except Exception:
                                pass

                        completed_tasks.append({
                            "id": task_id,
                            "type": task["type"],
                            "created_at": task["created_at"],
                            "priority": task["priority"],
                            "output": output,
                            "body_preview": task["body_preview"],
                        })
        else:
            # For worker agents (coder, research, qa, claude-code): scan outbox for _result.md files
            # Worker task .task.md files go to validation/, not outbox/
            if self.outbox.exists():
                for result_file in sorted(self.outbox.glob("*_result.md"), reverse=True):
                    try:
                        # Try to read metadata from result file
                        result_content = result_file.read_text(encoding="utf-8")

                        # Extract task ID from filename
                        filename = result_file.stem  # Remove .md extension
                        task_id = filename.replace("_result", "")  # task_id_result -> task_id

                        # Try to extract agent and type from result content metadata
                        # Result files have a simple YAML frontmatter with metadata
                        if result_content.startswith("---"):
                            parts = result_content.split("---", 2)
                            if len(parts) >= 2:
                                metadata = self._parse_yaml_frontmatter(parts[1].strip())
                                result_agent = metadata.get("agent", "")

                                # Include this result if it matches the requested agent
                                if result_agent == agent:
                                    output = result_content.split("---", 2)[-1].strip()[:2000] if len(parts) >= 3 else result_content[:2000]
                                    completed_tasks.append({
                                        "id": task_id,
                                        "type": metadata.get("type", "unknown"),
                                        "created_at": metadata.get("created_at", ""),
                                        "priority": "medium",  # Not stored in result metadata
                                        "output": output,
                                        "body_preview": "",
                                    })
                    except Exception:
                        pass

        # Scan failed for failed tasks by this agent
        if self.failed.exists():
            for task_file in sorted(self.failed.glob("*.task.md"), reverse=True):
                task = self._parse_task_file(task_file, "failed", "failed")
                if task and (task.get("assigned_to") == agent or (agent == "orchestrator" and task.get("assigned_to") == "orchestrator")):
                    # Try to read the result file
                    task_id = task["id"]
                    result_file = self.failed / f"{task_id}_result.md"
                    output = ""
                    if result_file.exists():
                        try:
                            output = result_file.read_text(encoding="utf-8")[:2000]
                        except Exception:
                            pass

                    failed_tasks.append({
                        "id": task_id,
                        "type": task["type"],
                        "created_at": task["created_at"],
                        "priority": task["priority"],
                        "output": output,
                        "body_preview": task["body_preview"],
                    })

        return {
            "agent": agent,
            "completed": completed_tasks[:50],  # Limit to last 50
            "failed": failed_tasks[:50],
        }
