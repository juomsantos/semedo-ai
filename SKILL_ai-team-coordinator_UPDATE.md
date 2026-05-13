---
name: ai-team-coordinator
description: "Coordinator workflow for building software projects using the AI Team multi-agent system. Use this skill whenever João wants to build, implement, or develop software using the AI Team agents — e.g. \"let's build a REST API\", \"implement this feature\", \"create a script that does X\", \"I want to start a new project with the AI team\", \"submit this to the agents\", \"check what the agents produced\". Also use when monitoring task progress, reviewing outbox results, or deciding what to submit next. If the AI Team folder is selected and the user wants to build something, always use this skill."
---

# AI Team — Coordinator Workflow

You are the master coordinator of a multi-agent AI development system. Your role is to translate João's software project goals into clear task descriptions, submit them to the orchestrator, monitor results, and iterate until the project is done. Delegate as much as possible — the agents exist to do the work.

## First: Orient Yourself

At the start of any session, read `CLAUDE.md` in the AI Team folder. It has the current system state, all agent details, and the full folder layout.

**Path references:**
- Windows (Read/Write/Edit tools): `C:\Users\JAAS\Desktop\AI Team\`
- Bash (mcp__workspace__bash): `find /sessions/*/mnt -maxdepth 1 -name "AI Team" -type d 2>/dev/null | head -1`

## The System at a Glance

**Agents (all started by `RUN_SCHEDULER.bat` or `python scripts/scheduler.py`):**

| Agent | Model | What it does |
|---|---|---|
| Orchestrator | qwen3.5:9b | Receives all tasks from `inbox/`, routes and decomposes them |
| Coder | qwen3.5:9b | Code generation — Python, JS/TS, C#/.NET, Java, and others |
| Research | qwen3.5:9b | Architecture decisions, tech comparisons, documentation, analysis |
| Claude Code | claude CLI | Complex multi-step tasks, escalation from local models |
| QA | qwen3.5:9b | Runs and reviews all code; one auto-retry before writing to `failed/` |

**Triggering:** agents are triggered immediately by a file watcher when `.task.md` files appear in their inboxes. Timer-based polling is currently disabled (`config.json → scheduler.enable_timer_polling: false`).

**You always submit to `inbox/` — never directly to a worker inbox.** The orchestrator owns all routing and decomposition decisions, including whether to involve the research agent before coding. Your job is to describe what to build clearly; the orchestrator decides who does what and in what order.

**Remind João to start the scheduler** before submitting tasks if it isn't already running (`RUN_SCHEDULER.bat`).

## Phase 1: Project Intake

Understand the project well enough to write a clear, complete task description. Clarify:
- **What to build** — feature, module, app, script?
- **Language / framework** — state it explicitly; infer from context if possible
- **Scope** — single function, a module, a full service?
- **Constraints** — dependencies, existing code to integrate with, performance requirements
- **Done criteria** — tests? a running service? a CLI with specific behaviour?

Don't over-interview. A sentence or two is usually enough for small tasks.

## Phase 2: Task Decomposition

**Default: submit one task.** Describe the full goal clearly — what to build, in what language/framework, with what constraints — and let the orchestrator decide how to break it down, whether to do research first, and which agents to involve. That's its job.

The orchestrator supports a **research-first decomposition** mode (`redecompose_after_research`): if its LLM decides research must happen before it can produce a good implementation plan, it will dispatch only the research subtask first, then re-decompose with the research result as context before dispatching implementation subtasks. You don't need to do anything special to trigger this — just describe the goal and the orchestrator will choose this path when appropriate.

Everything else — routing, parallel work, subtask splitting — belongs to the orchestrator. When in doubt, submit one task and let it decide.

## Phase 3: Writing Task Files

Use the `create_task_file` helper — it handles ID generation and frontmatter automatically.

First, find the bash mount path:
```bash
AI_TEAM=$(find /sessions/*/mnt -maxdepth 1 -name "AI Team" -type d 2>/dev/null | head -1)
echo "$AI_TEAM"
```

Then submit:
```bash
cd "$AI_TEAM"
python3 - <<'EOF'
import sys
sys.path.insert(0, "scripts")
from shared.task_io import create_task_file
from pathlib import Path

task_path = create_task_file(
    inbox_path=Path("inbox"),
    task_type="code",          # code | research | summarize | review | plan
    description="""
Write a Python CLI tool that...

## Expected Output
A working script at scripts/my_tool.py that...
""",
    expected_output="A working Python script.",
    priority="medium",         # high | medium | low
    created_by="claude-cowork",
    assigned_to="orchestrator",
)
print(f"Submitted: {task_path.stem}")
EOF
```

Or use the dashboard **Submit Task** tab at `http://localhost:5000` — no Python needed.

## Phase 4: Monitoring

Check pipeline state at any time:
```bash
AI_TEAM=$(find /sessions/*/mnt -maxdepth 1 -name "AI Team" -type d 2>/dev/null | head -1)

# Quick counts
echo "=== INBOX ===" && ls "$AI_TEAM/inbox/"*.task.md 2>/dev/null | wc -l
echo "=== PROCESSING ===" && ls "$AI_TEAM/processing/"*.task.md 2>/dev/null | wc -l
echo "=== VALIDATION ===" && ls "$AI_TEAM/validation/"*.task.md 2>/dev/null | wc -l
echo "=== OUTBOX ===" && ls "$AI_TEAM/outbox/"*.task.md 2>/dev/null | wc -l
echo "=== FAILED ===" && ls "$AI_TEAM/failed/"*.task.md 2>/dev/null | wc -l

# Worker inboxes
for agent in coder research qa; do
  echo "=== $agent inbox ===" && ls "$AI_TEAM/agents/$agent/inbox/"*.task.md 2>/dev/null | wc -l
done
```

Or view the real-time dashboard at `http://localhost:5000` (start with `python dashboard/run_dashboard.py`).

**Task lifecycle:** `inbox/` → `processing/` (orchestrator decomposes) → `agents/*/inbox/` (worker executes) → `validation/` (awaiting orchestrator sign-off) → `outbox/` (complete) or `failed/` (error).

## Phase 5: Reading Results

Once a task reaches `outbox/`, read the parent result file (aggregates all subtask outputs):

```bash
AI_TEAM=$(find /sessions/*/mnt -maxdepth 1 -name "AI Team" -type d 2>/dev/null | head -1)
# List completed result files, newest first
ls -t "$AI_TEAM/outbox/"*_result.md 2>/dev/null | head -10

# Read a specific result
cat "$AI_TEAM/outbox/task_20260513_120000_000000_result.md"
```

Use the Read tool for longer results: `C:\Users\JAAS\Desktop\AI Team\outbox\<task_id>_result.md`

## Phase 6: Handling Failed Tasks

Failed tasks land in `failed/` — either QA rejected after two attempts, or an infrastructure error:

```bash
AI_TEAM=$(find /sessions/*/mnt -maxdepth 1 -name "AI Team" -type d 2>/dev/null | head -1)
ls -t "$AI_TEAM/failed/"*.md 2>/dev/null | head -10
cat "$AI_TEAM/failed/<task_id>_result.md"
```

Common causes and actions:
- **QA FAIL x2** — Read the failure report for the specific code issues. Resubmit the task with the feedback incorporated into the description, or submit a targeted fix task.
- **Stall (Ollama timeout)** — The orchestrator's `recover_stalled_subtasks()` retries automatically (up to 2 times). If still failing, check Ollama server availability and resubmit.
- **Orchestrator decomposition error** — Check orchestrator logs in `logs/orchestrator/general.log`, then resubmit.

## Phase 7: Claude Code Approvals

Tasks routed to `claude-code` require manual approval before they run:

**Via dashboard:** open `http://localhost:5000` → **Approvals** tab → review the task body → click Approve or Reject.

**Manually:** move the file from `agents/claude-code/pending/` to `agents/claude-code/inbox/`.

Only approve tasks you've reviewed — the claude CLI runs with full access to the project.

## Key Paths Reference

| What | Windows path | Bash path |
|---|---|---|
| Task inbox | `C:\Users\JAAS\Desktop\AI Team\inbox\` | `$AI_TEAM/inbox/` |
| Results | `C:\Users\JAAS\Desktop\AI Team\outbox\` | `$AI_TEAM/outbox/` |
| Failed | `C:\Users\JAAS\Desktop\AI Team\failed\` | `$AI_TEAM/failed/` |
| Orchestrator log | `C:\Users\JAAS\Desktop\AI Team\logs\orchestrator\general.log` | `$AI_TEAM/logs/orchestrator/general.log` |
| Coder log | `C:\Users\JAAS\Desktop\AI Team\logs\coder\general.log` | `$AI_TEAM/logs/coder/general.log` |
| Dashboard | `http://localhost:5000` | — |

## Troubleshooting

**Scheduler not running?**
```bash
cat "$AI_TEAM/logs/scheduler/general.log" | tail -20
```
Start it: run `RUN_SCHEDULER.bat` (Windows) or `python scripts/scheduler.py` (terminal).

**Tasks stuck in inbox?**
- Scheduler may not be running — check above.
- File watcher may have missed the file — if timer polling is disabled, the watcher is the only trigger. Restart the scheduler.

**Tasks stuck in processing for a long time?**
The orchestrator's `recover_processing_subtasks()` function handles workers killed mid-call (12-minute threshold). After the next orchestrator cycle the stuck task should be returned to the worker inbox automatically.

**Ollama not reachable?**
```bash
curl -s http://192.168.1.13:11434/api/tags | head -5
```
Agents will fail on LLM calls until Ollama is back up. Tasks remain in their folders and will be picked up once Ollama recovers.
