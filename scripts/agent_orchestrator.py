"""
agent_orchestrator.py — Orchestrator agent (qwen3:9b).

CRON: */1 * * * * /usr/bin/python3 /path/to/scripts/agent_orchestrator.py

Responsibilities:
  1. Poll inbox/ for pending .task.md files
  2. For each task, call qwen3:9b to:
     a. Decide if task should be decomposed into subtasks
     b. Route each (sub)task to the correct worker agent
  3. Write subtasks to agents/<worker>/inbox/
  4. Move original task to processing/ (then outbox/ when all subtasks complete)
  5. Log all decisions to logs/orchestrator/

Worker agents available:
  - coder       → agents/coder/inbox/        (qwen2.5-coder:7b)
  - research     → agents/research/inbox/     (qwen3:9b)
  - claude-code  → agents/claude-code/inbox/  (claude CLI)

Routing rules (enforced via system prompt, not hardcoded):
  - Code generation, debugging, refactoring → coder
  - Research, summarization, Q&A, writing   → research
  - Complex multi-step reasoning, anything requiring tool use → claude-code
  - Unknown/ambiguous → research (safest fallback)
"""

import sys
import json
import os
import atexit
from pathlib import Path

# Allow importing from shared/ regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.task_io import (
    list_pending_tasks,
    read_task,
    mark_processing,
    mark_completed,
    mark_failed,
    create_task_file,
    resolve_task_dependencies,
    list_validation_tasks,
    get_completed_subtasks_by_parent,
    write_result,
    PROJECT_ROOT,
)
from shared.token_logger import log_tokens
from shared.ollama_client import OllamaClient, OllamaError
from shared.logger import AgentLogger
from shared.config import load_config

AGENT_NAME = "orchestrator"
_config = load_config()
MODEL = _config.agent_model(AGENT_NAME)
INBOX = PROJECT_ROOT / "inbox"
LOCK_FILE = PROJECT_ROOT / "processing" / "orchestrator.lock"


# ---------------------------------------------------------------------------
# Concurrency guard — prevents two orchestrator instances running at once
# ---------------------------------------------------------------------------

def _pid_exists(pid: int) -> bool:
    """Cross-platform check: is this PID still alive?"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def acquire_lock(log: "AgentLogger") -> bool:
    """
    Try to write a lockfile containing our PID.
    Returns True if acquired, False if another instance is already running.
    Stale locks (dead PID) are removed automatically.
    """
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_exists(pid):
                log.info(f"Orchestrator already running (PID {pid}) — skipping this run")
                return False
            else:
                log.warning(f"Removing stale lockfile (PID {pid} no longer running)")
                LOCK_FILE.unlink()
        except Exception:
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile. Registered with atexit so it runs on clean exit or exception."""
    LOCK_FILE.unlink(missing_ok=True)


SYSTEM_PROMPT_PATH = PROJECT_ROOT / "agents" / "orchestrator" / "system_prompt.md"
VALIDATION_PROMPT_PATH = PROJECT_ROOT / "agents" / "orchestrator" / "validation_system_prompt.md"

WORKER_INBOXES = {
    "coder": PROJECT_ROOT / "agents" / "coder" / "inbox",
    "research": PROJECT_ROOT / "agents" / "research" / "inbox",
    "claude-code": PROJECT_ROOT / "agents" / "claude-code" / "inbox",
    "pending_approval": PROJECT_ROOT / "agents" / "claude-code" / "pending",
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def load_validation_prompt() -> str:
    return VALIDATION_PROMPT_PATH.read_text(encoding="utf-8")


def parse_routing_decision(response: str) -> list[dict]:
    """
    Parse the LLM's routing decision from its response.
    Expected format is a JSON array of subtask objects:
    [
      {
        "worker": "coder"|"research"|"claude-code",
        "type": "code"|"research"|"summarize"|...,
        "description": "...",
        "expected_output": "..."
      },
      ...
    ]
    TODO: Implement robust parsing with fallback to single-task routing.
    """
    import re

    # Try to extract JSON from markdown code fences (```json ... ```)
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from response: {e}")

    # Ensure it's a list
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    if not data:
        raise ValueError("Routing decision array is empty")

    # Validate each subtask
    valid_workers = {"coder", "research", "claude-code", "pending_approval"}
    for i, subtask in enumerate(data):
        if not isinstance(subtask, dict):
            raise ValueError(f"Subtask {i} is not a dict: {type(subtask).__name__}")

        required_fields = {"worker", "type", "description", "expected_output"}
        missing = required_fields - set(subtask.keys())
        if missing:
            raise ValueError(f"Subtask {i} missing required fields: {missing}")

        if subtask["worker"] not in valid_workers:
            raise ValueError(f"Subtask {i} has invalid worker '{subtask['worker']}' (valid: {valid_workers})")

    return data


def parse_validation_decision(response: str) -> dict:
    """
    Parse the orchestrator's validation decision.
    Expected format is a JSON object with:
    {
      "decision": "complete|refine|additional_work|redo",
      "reasoning": "...",
      "follow_ups": [...]  # Only if decision != "complete"
    }
    """
    import re

    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
    json_str = json_match.group(1) if json_match else response.strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse validation JSON: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    decision = data.get("decision")
    valid_decisions = {"complete", "refine", "additional_work", "redo"}
    if decision not in valid_decisions:
        raise ValueError(f"Invalid decision '{decision}' (valid: {valid_decisions})")

    return data


def validate_completed_tasks(parent_task_id: str, completed_subtasks: list, client: OllamaClient, log: AgentLogger):
    """
    Call orchestrator LLM to validate completed subtasks.
    Returns decision: "complete", "refine", "additional_work", or "redo"
    """
    # Read original parent task from processing/ or outbox/
    processing_dir = PROJECT_ROOT / "processing"
    parent_path = None
    for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
        parent_path = candidate
        break

    if not parent_path:
        log.error(f"Cannot find parent task {parent_task_id} — moving orphaned subtasks to failed/")
        for subtask in completed_subtasks:
            try:
                mark_failed(subtask["path"])
                log.warning(f"Moved orphaned subtask {Path(subtask['path']).name} to failed/ (parent {parent_task_id} not found)")
            except Exception as e:
                log.error(f"Failed to move orphaned subtask to failed/: {e}")
        return None

    parent_task = read_task(parent_path)

    # Build validation prompt with parent task + completed results
    validation_prompt = load_validation_prompt()

    # Format completed subtasks with their results
    subtask_results = []
    for subtask in completed_subtasks:
        task_id = subtask["meta"].get("id")
        task_type = subtask["meta"].get("type")
        output_path = subtask["meta"].get("output_path")

        # Read the actual result if it exists
        result_content = ""
        if output_path and Path(output_path).exists():
            result_content = Path(output_path).read_text(encoding="utf-8")[:1000]  # First 1000 chars

        subtask_results.append({
            "task_id": task_id,
            "type": task_type,
            "assigned_to": subtask["meta"].get("assigned_to"),
            "body_preview": subtask["body"][:300],
            "result_preview": result_content,
        })

    # Iteration count (for loop prevention)
    iteration = parent_task["meta"].get("iteration", 1)
    max_iterations = 5

    if iteration >= max_iterations:
        log.warning(f"Task {parent_task_id} reached max iterations ({max_iterations}) — forcing completion")
        return {
            "decision": "complete",
            "reasoning": f"Max iterations ({max_iterations}) reached. Completing task to prevent infinite loop."
        }

    user_message = f"""## Parent Task
ID: {parent_task_id}
Type: {parent_task['meta'].get('type')}
Description:
{parent_task['body']}

## Completed Subtasks (Iteration {iteration}/{max_iterations})
{json.dumps(subtask_results, indent=2)}

Evaluate these results and decide if the work is complete. You have {max_iterations - iteration} iteration(s) remaining."""

    try:
        response = client.chat(model=MODEL, system_prompt=validation_prompt, user_message=user_message)
        log_tokens(AGENT_NAME, parent_task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        log.info(f"Validation response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error during validation of {parent_task_id}: {e}")
        return None

    try:
        decision = parse_validation_decision(response)
    except Exception as e:
        log.error(f"Failed to parse validation decision for {parent_task_id}: {e}")
        log.error(f"Raw response: {response[:500]}")
        return None

    return decision


def recover_orphaned_tasks(log: AgentLogger):
    """
    Move any tasks stuck in processing/ with status:pending back to inbox/
    so they can be re-dispatched. These are tasks the orchestrator started
    but never finished decomposing (e.g. killed mid-LLM-call).
    """
    processing_dir = PROJECT_ROOT / "processing"
    if not processing_dir.exists():
        return
    for task_file in processing_dir.glob("*.task.md"):
        try:
            task = read_task(task_file)
            if task["meta"].get("status") == "pending":
                dest = INBOX / task_file.name
                task_file.rename(dest)
                log.warning(f"Recovered orphaned task {task_file.name} → inbox/")
        except Exception as e:
            log.error(f"Error inspecting {task_file.name} during recovery: {e}")


def process_task(task: dict, client: OllamaClient, log: AgentLogger):
    """Route or decompose a single task."""
    task_id = task["meta"].get("id", "unknown")
    log.info(f"Processing task {task_id}")

    # Move to processing immediately so other cron invocations skip it
    task_path = mark_processing(task["path"])
    log.info(f"Moved to processing: {task_path}")

    system_prompt = load_system_prompt()
    # Convert datetime objects to ISO strings for JSON serialization
    meta_for_json = {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in task['meta'].items()}
    user_message = f"---\n{json.dumps(meta_for_json, indent=2)}\n---\n\n{task['body']}"

    try:
        response = client.chat(model=MODEL, system_prompt=system_prompt, user_message=user_message)
        log_tokens(AGENT_NAME, task_id, client.last_token_counts["prompt"], client.last_token_counts["completion"])
        log.info(f"Orchestrator LLM response received ({len(response)} chars)")
    except OllamaError as e:
        log.error(f"Ollama error for {task_id}: {e}")
        mark_failed(task_path)
        return

    try:
        subtasks = parse_routing_decision(response)
    except Exception as e:
        log.error(f"Failed to parse routing decision for {task_id}: {e}")
        log.error(f"Raw response: {response[:500]}")
        mark_failed(task_path)
        return

    # Track created subtasks to build dependency graph
    created_subtasks = {}

    for subtask in subtasks:
        worker = subtask["worker"]

        # Handle pending_approval routing target
        if worker == "pending_approval":
            pending_inbox = PROJECT_ROOT / "agents" / "claude-code" / "pending"
            pending_inbox.mkdir(parents=True, exist_ok=True)
            new_task_path = create_task_file(
                inbox_path=pending_inbox,
                task_type=subtask["type"],
                description=subtask["description"],
                expected_output=subtask["expected_output"],
                assigned_to="pending_approval",
                created_by=AGENT_NAME,
                parent_task_id=task_id,
            )
            log.info(f"Created pending task {new_task_path.name} → pending_approval")
            continue

        inbox = WORKER_INBOXES.get(worker)
        if not inbox:
            log.error(f"Unknown worker '{worker}' in routing decision — skipping subtask")
            continue

        # Code tasks should chain to QA for review
        chain_to = "qa" if subtask["type"] == "code" else None
        original_description = subtask["description"] if subtask["type"] == "code" else None

        new_task_path = create_task_file(
            inbox_path=inbox,
            task_type=subtask["type"],
            description=subtask["description"],
            expected_output=subtask["expected_output"],
            assigned_to=worker,
            created_by=AGENT_NAME,
            chain_to=chain_to,
            original_description=original_description,
            parent_task_id=task_id,
        )

        # Track subtask for dependency wiring
        created_subtasks[worker] = new_task_path
        log.info(f"Created subtask {new_task_path.name} → {worker}")

    # Wire dependencies: if both research and code tasks exist, code depends on research
    if "research" in created_subtasks and "coder" in created_subtasks:
        research_path = created_subtasks["research"]
        coder_path = created_subtasks["coder"]
        research_task = read_task(research_path)

        # Read the coder task and add research output as dependency
        coder_task = read_task(coder_path)
        coder_task["meta"]["depends_on"] = [research_task["meta"]["id"]]

        # Re-write coder task with dependency info
        coder_body = coder_task["body"]
        write_result(str(coder_path), coder_body, meta=coder_task["meta"])
        log.info(f"Wired dependency: coder {coder_path.name} depends on research {research_path.name}")

    # Update parent task status to "dispatched" so orphan recovery does not
    # re-dispatch it on the next cycle (recover_orphaned_tasks checks status=="pending")
    try:
        parent_task = read_task(task_path)
        parent_task["meta"]["status"] = "dispatched"
        write_result(str(task_path), parent_task["body"], meta=parent_task["meta"])
        log.info(f"Parent task {task_id} status updated to 'dispatched'")
    except Exception as e:
        log.error(f"Failed to update parent task status for {task_id}: {e}")

    log.info(f"Task {task_id} dispatched — awaiting subtask completion in validation loop")


def handle_validation_decision(parent_task_id: str, decision: dict, client: OllamaClient, log: AgentLogger):
    """
    Process the validation decision:
    - "complete": Mark parent task as complete
    - "refine"/"additional_work": Create follow-up tasks
    - "redo": Create new subtasks with failure context
    """
    decision_type = decision.get("decision")
    reasoning = decision.get("reasoning", "No reasoning provided")

    log.info(f"Validation decision for {parent_task_id}: {decision_type}")
    log.info(f"Reasoning: {reasoning}")

    # Get current iteration from parent task
    processing_dir = PROJECT_ROOT / "processing"
    parent_path = None
    for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
        parent_path = candidate
        break

    current_iteration = 1
    if parent_path:
        parent_task = read_task(parent_path)
        current_iteration = parent_task["meta"].get("iteration", 1)

    # Handle follow-up task creation if needed
    follow_ups = decision.get("follow_ups", [])
    if follow_ups:
        log.info(f"Creating {len(follow_ups)} follow-up task(s) for iteration {current_iteration + 1}")
        for idx, followup in enumerate(follow_ups):
            worker = followup.get("worker")
            inbox = WORKER_INBOXES.get(worker)
            if not inbox:
                log.error(f"Unknown worker '{worker}' in follow-up — skipping")
                continue

            chain_to = "qa" if followup.get("type") == "code" else None
            new_task_path = create_task_file(
                inbox_path=inbox,
                task_type=followup.get("type"),
                description=followup.get("description"),
                expected_output=followup.get("expected_output"),
                assigned_to=worker,
                created_by=AGENT_NAME,
                parent_task_id=parent_task_id,
                chain_to=chain_to,
            )
            # Note: iteration gets incremented in parent task metadata when it re-enters validation
            log.info(f"Created follow-up task {new_task_path.name} → {worker}")

    if decision_type == "complete":
        # Mark parent task as truly complete (move from processing to outbox)
        processing_dir = PROJECT_ROOT / "processing"
        parent_path = None
        for candidate in processing_dir.glob(f"{parent_task_id}.task.md"):
            parent_path = candidate
            break

        if parent_path and parent_path.exists():
            mark_completed(parent_path)
            log.info(f"Task {parent_task_id} APPROVED and marked complete")

    elif decision_type in ["refine", "additional_work", "redo"]:
        # Follow-ups have been created; update parent's iteration counter and keep in processing
        if parent_path and parent_path.exists():
            parent_task = read_task(parent_path)
            current_iter = parent_task["meta"].get("iteration", 1)
            parent_task["meta"]["iteration"] = current_iter + 1
            parent_task["meta"]["last_validation"] = decision_type
            # Update the parent task file with new iteration
            write_result(str(parent_path), parent_task["body"], meta=parent_task["meta"])
            log.info(f"Task {parent_task_id} iteration incremented to {current_iter + 1}")

        log.info(f"Task {parent_task_id} needs more work — follow-ups created, awaiting next iteration")


def validation_phase(client: OllamaClient, log: AgentLogger):
    """
    Validate all completed subtasks waiting in the validation folder.
    Group by parent task and call orchestrator LLM to decide: complete or create follow-ups.
    """
    validation_dir = PROJECT_ROOT / "validation"
    if not validation_dir.exists():
        return

    grouped = get_completed_subtasks_by_parent(validation_dir)
    if not grouped:
        log.info("No tasks awaiting validation")
        return

    log.info(f"Found {len(grouped)} parent task(s) with completed subtasks awaiting validation")

    for parent_task_id, completed_subtasks in grouped.items():
        log.info(f"Validating {len(completed_subtasks)} subtask(s) for parent {parent_task_id}")

        decision = validate_completed_tasks(parent_task_id, completed_subtasks, client, log)
        if decision:
            handle_validation_decision(parent_task_id, decision, client, log)


def main():
    log = AgentLogger(AGENT_NAME)

    if not acquire_lock(log):
        return  # Another instance is running — exit cleanly
    atexit.register(release_lock)  # Ensure lock is released on any exit

    recover_orphaned_tasks(log)

    client = OllamaClient()

    if not client.is_available():
        log.error(f"Ollama is not reachable at {client.base_url} — aborting")
        sys.exit(1)

    # Phase 1: Validate completed tasks
    log.info("=== VALIDATION PHASE ===")
    validation_phase(client, log)

    # Phase 2: Resolve pending task dependencies
    agent_inboxes = {
        "coder": PROJECT_ROOT / "agents" / "coder" / "inbox",
        "research": PROJECT_ROOT / "agents" / "research" / "inbox",
        "claude-code": PROJECT_ROOT / "agents" / "claude-code" / "inbox",
        "qa": PROJECT_ROOT / "agents" / "qa" / "inbox",
    }
    log.info("=== DEPENDENCY RESOLUTION PHASE ===")
    resolve_task_dependencies(agent_inboxes)
    log.info("Dependency resolution complete")

    # Phase 3: Decompose and dispatch new tasks
    log.info("=== DISPATCH PHASE ===")
    tasks = list_pending_tasks(INBOX)
    if not tasks:
        log.info("Inbox empty — nothing to do")
        return

    log.info(f"Found {len(tasks)} pending task(s)")
    for task_path in tasks:
        try:
            task = read_task(task_path)
            process_task(task, client, log)
        except Exception as e:
            log.error(f"Unhandled error processing {task_path.name}: {e}")


if __name__ == "__main__":
    main()
