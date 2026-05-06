# QA Agent — Implementation Briefing for Claude Code

Read `CLAUDE.md`, `ARCHITECTURE.md`, and `IMPLEMENTATION_COMPLETE.md` first for full system context.

---

## What to Build

Add a QA agent that automatically reviews and tests code produced by the coder agent.

---

## Behaviour Spec

### Flow

```
Orchestrator routes a code task → sets chain_to: qa, retry_count: 0
        ↓
Coder produces output → sees chain_to: qa → creates a QA task in agents/qa/inbox/
        ↓
QA agent (every 2 min poll):
  1. Extract Python code from the coder's result file (strip markdown fences)
  2. Execute the code via subprocess with a 30s timeout, capture stdout/stderr/exit code
  3. Call qwen3.5:9b with: original task description + code + execution output
  4. LLM returns verdict: PASS or FAIL with written feedback

  IF PASS  → move result to outbox/, log success
  IF FAIL AND retry_count == 0 →
      create a new coder task with: original description + QA feedback as context
      set chain_to: qa and retry_count: 1 on the new task
  IF FAIL AND retry_count == 1 →
      write a detailed failure report to failed/ (includes: code, execution output, QA review)
      log final failure
```

### Model
- QA agent uses `qwen3.5:9b` (stronger reasoning for intent vs implementation analysis)

---

## Files to Create

### 1. `scripts/agent_qa.py`
Poll `agents/qa/inbox/` every 2 minutes. Follow the exact same structure as `agent_coder.py` and `agent_research.py` — same import pattern, same task lifecycle (mark_processing → work → mark_completed or mark_failed).

Key function to implement:
```python
def extract_code(result_content: str) -> str:
    """Strip markdown fences from result file content to get raw Python."""

def execute_code(code: str, log) -> dict:
    """Run code via subprocess. Return {stdout, stderr, exit_code, timed_out}."""
    # Use subprocess.run with timeout=30, capture_output=True
    # Write code to a temp file, run with sys.executable

def review_with_llm(task_description: str, code: str, execution: dict, client, log) -> dict:
    """Call qwen3.5:9b to review. Return {verdict: 'PASS'|'FAIL', feedback: str}."""

def handle_failure(task: dict, feedback: str, code: str, execution: dict, log):
    """Either retry (retry_count==0) or write to failed/ (retry_count==1)."""
```

### 2. `agents/qa/system_prompt.md`
Instruct the LLM to review code given: task description, code, and execution output.
Output must be structured exactly as:
```
VERDICT: PASS
```
or:
```
VERDICT: FAIL
FEEDBACK:
<specific, actionable feedback for the coder>
```
No other format accepted.

---

## Files to Modify

### 3. `scripts/shared/task_io.py` — add two optional params to `create_task_file()`
```python
def create_task_file(
    ...
    chain_to: str | None = None,      # e.g. "qa" — worker to chain to after completion
    retry_count: int = 0,             # how many QA retries have occurred
    original_description: str | None = None,  # preserve original task intent across retries
) -> Path:
```
Write these fields into the YAML frontmatter.

### 4. `scripts/agent_coder.py` — add chaining after task completion
After writing the result file and before calling `mark_completed()`:
```python
chain_to = task["meta"].get("chain_to")
if chain_to == "qa":
    create_task_file(
        inbox_path=PROJECT_ROOT / "agents" / "qa" / "inbox",
        task_type="qa",
        description=task["meta"].get("original_description") or task["body"],
        expected_output="QA verdict: PASS or FAIL with feedback",
        assigned_to="qa",
        created_by=AGENT_NAME,
        chain_to=None,
        retry_count=task["meta"].get("retry_count", 0),
        original_description=task["meta"].get("original_description") or task["body"],
        context_files=[output_path],  # point QA at the coder's result file
    )
    log.info("Chained to QA agent")
```

### 5. `scripts/agent_orchestrator.py` — set chain_to for code tasks
In `process_task()`, when writing subtasks via `create_task_file()`, pass `chain_to="qa"` and `original_description=subtask["description"]` if the subtask type is `code`.

### 6. `scripts/scheduler.py` — add QA to the agent list
```python
AGENTS = [
    ("agent_orchestrator.py", 1),
    ("agent_coder.py", 2),
    ("agent_research.py", 2),
    ("agent_claude_code.py", 3),
    ("agent_qa.py", 2),   # ADD THIS
]
```

---

## Folder to Create

```bash
mkdir -p agents/qa/inbox
```

---

## Testing

After implementation, run an end-to-end test:

1. Create a code task with a deliberate bug (e.g. divide by zero, wrong return type)
2. Run orchestrator → coder → QA manually in sequence
3. Confirm QA fails, creates a retry coder task
4. Run coder again with the fixed prompt → QA again
5. Confirm second pass succeeds OR writes a report to `failed/`

Also test the happy path: drop a clean code task and confirm it flows all the way to outbox with a QA PASS.

---

## Key Constraints

- QA must never modify the coder's result file — it only reads it
- Code execution must use a temp file (not eval/exec) and always have a timeout
- The retry loop must be strictly limited to 1 retry — check `retry_count` from task frontmatter
- Follow the exact same agent structure as existing agents (imports, logging, task lifecycle)
- Do not add new dependencies — `subprocess`, `tempfile`, `re` are all stdlib
