# AI Team — Multi-Agent Architecture

> Last updated: 2026-05-10

## Overview

A team of agents coordinated through this shared folder. Agents poll their inboxes on a schedule (via `scheduler.py`) and communicate exclusively through structured task files. Ollama runs at `http://192.168.1.13:11434`.

## Topology

```
[João / Claude (Cowork)]
        │  writes tasks
        ▼
   inbox/
        │
        ▼  polls every 3 min — 3 phases per run
[Orchestrator: qwen3.5:9b]
   Phase 1 — VALIDATION: scan validation/, group by parent, LLM decides complete|refine|additional_work|redo
   Phase 2 — DEPENDENCY RESOLUTION: unblock tasks whose depends_on are satisfied, wire context_files
   Phase 3 — DISPATCH: decompose new inbox tasks, route to worker inboxes
        │
   ┌────┼────────────────────┐
   ▼    ▼                    ▼
[Coder]  [Research]   [Claude Code*]
qwen2.5  qwen3.5:9b    claude CLI
coder:7b  + web search   *requires approval
   │ chain_to: qa
   ▼
[QA Agent: qwen3.5:9b + web search]
   - Extracts code, executes via subprocess (30s timeout)
   - Reviews with qwen3.5:9b; may search for error/library docs
   - PASS → validation/  (awaits orchestrator sign-off)
   - FAIL (retry_count=0) → new coder task with feedback
   - FAIL (retry_count=1) → failure report to failed/
        │
        ▼
   validation/  ←── all workers land here
        │
        ▼  (orchestrator Phase 1)
   [Orchestrator validates — complete | refine | redo | additional_work]
        │
   ┌────┴────────┐
   ▼             ▼
outbox/       failed/         (max 5 validation iterations per task)
        │
        ▼
[Claude (Cowork) reviews & delivers to João]
```

## Folder Structure

```
AI Team/
  ARCHITECTURE.md              ← this file
  CLAUDE.md                    ← project instructions for Claude (Cowork)
  DASHBOARD.md                 ← dashboard usage and REST API reference
  config.json                  ← runtime config (Ollama URL, models, dashboard port)
  RUN_SCHEDULER.bat            ← Windows quick-start (agents only)
  RUN_SCHEDULER.sh             ← Linux/Mac quick-start
  inbox/                       ← drop task files here to start work
  processing/                  ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/                  ← completed subtasks awaiting orchestrator approval
  outbox/                      ← approved & truly completed results
  failed/                      ← QA failure reports + hard-errored tasks
  context/                     ← optional shared context files for tasks
  agents/
    orchestrator/
      system_prompt.md         ← decomposition & routing prompt (3 decision types)
      validation_system_prompt.md ← validation decision prompt (complete|refine|additional_work|redo)
    coder/
      inbox/
      system_prompt.md
    research/
      inbox/
      system_prompt.md
    claude-code/
      inbox/                   ← approved tasks ready to run
      pending/                 ← tasks awaiting manual approval before claude-code runs
    qa/
      inbox/
      system_prompt.md
  dashboard/                   ← real-time web monitoring UI (Flask)
    app.py                     ← REST API server
    run_dashboard.py           ← launcher (reads config.json)
    task_monitor.py            ← filesystem scanner
    templates/index.html       ← dashboard UI
    static/dashboard.js        ← frontend polling logic
    static/dashboard.css       ← styling
  logs/                        ← per-agent execution traces at logs/<agent>/general.log
  scripts/
    shared/
      task_io.py               ← task file I/O: read/write/move, dependency resolution, validation grouping
      ollama_client.py         ← Ollama REST wrapper: chat() and chat_with_tools(); stores last_token_counts after each call
      token_logger.py          ← appends {ts, task_id, prompt, completion} to logs/<agent>/tokens.jsonl
      web_search.py            ← DuckDuckGo search wrapper (research + QA agents)
      logger.py                ← file + stdout logging, UTC timestamps
      config.py                ← config.json loader (ProjectConfig class)
    agent_orchestrator.py      ← 3-phase loop: validate, resolve deps, dispatch
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py               ← cross-platform Python polling scheduler
```

## Task File Format

```markdown
---
id: task_YYYYMMDD_HHMMSS_microseconds   ← microseconds suffix prevents ID collisions
type: research|code|summarize|review|plan|qa
priority: high|medium|low
created_by: claude-cowork|orchestrator|coder|qa
created_at: 2026-05-07T10:00:00
assigned_to: orchestrator|coder|research|claude-code|qa|pending_approval
status: pending|dispatched|processing   ← pending=awaiting orchestrator; dispatched=subtasks created, waiting validation; processing=worker running it
output_path: outbox/task_..._result.md
context_files: []             ← populated by dependency resolver when deps complete
parent_task_id: task_...      ← links subtask back to original parent (set by orchestrator)
depends_on: [task_...]        ← blocks processing until listed tasks land in outbox/
chain_to: qa                  ← optional: agent to hand off to after completion
retry_count: 0                ← QA retry counter (max 1 before escalating to failed/)
original_description: ...     ← preserved across retries for QA context
iteration: 1                  ← validation loop iteration counter (max 5, on parent tasks)
---

## Task Description
...

## Expected Output
...
```

## Result File Format

Result files are written to `outbox/` by workers and the orchestrator. Each `.task.md` file has a corresponding `*_result.md` file containing the deliverable output.

**Subtask Result** (`outbox/task_..._result.md` — written by worker agent):

```markdown
---
task_id: task_YYYYMMDD_HHMMSS_microseconds
agent: research|coder|qa|claude-code
model: qwen3.5:9b|qwen2.5-coder:7b|Claude (...)
---

# Result Title

[Worker-generated output: code, research report, QA verdict, etc.]
```

**Parent Task Result** (`outbox/task_parent_result.md` — written by orchestrator after validation):

When a parent task completes, the orchestrator aggregates all approved subtask results into a single parent result file. The format includes the orchestrator's validation summary, followed by the actual deliverables:

```markdown
---
task_id: task_YYYYMMDD_HHMMSS_microseconds
status: complete
---

# Task Completion Summary

Task {parent_task_id} completed after validation.

## Decision Reasoning

{Orchestrator's validation rationale — why this task was approved}

## Subtask Results

### Research Result (Task: task_...)

[Full research output from research subtask]

### Code Result (Task: task_...)

[Full code/implementation from coder subtask]

### Qa Result (Task: task_...)

[Full QA verdict, execution results, and review from QA subtask]
```

**Result Aggregation Rules:**

- Parent results always include decision reasoning (why it was approved)
- All approved subtask outputs are aggregated in a single parent result file
- Subtasks are ordered by type: research → code → qa → other types
- Each subtask result is included in full (not summarized or truncated)
- Result files correspond to what was originally requested: if code was requested, the code appears; if research was requested, the research findings appear
- Missing result files are handled gracefully with a `[Result file not found: ...]` placeholder

This ensures that when reviewing a completed task, all deliverables are available in one place rather than scattered across multiple subtask files.

## Agent Scripts

All scripts live in `scripts/`. Each is standalone and invoked by the scheduler.

| Script | Model | Inbox | Interval | Notes |
|---|---|---|---|---|
| `agent_orchestrator.py` | qwen3.5:9b | `inbox/` | 3 min | 3-phase loop per run |
| `agent_coder.py` | qwen2.5-coder:7b | `agents/coder/inbox/` | 2 min | skips tasks with unresolved `depends_on` |
| `agent_research.py` | qwen3.5:9b | `agents/research/inbox/` | 2 min | web search via DuckDuckGo (max 5 searches/task) |
| `agent_claude_code.py` | Claude Code CLI | `agents/claude-code/inbox/` | 3 min | tasks arrive via manual approval from `pending/` |
| `agent_qa.py` | qwen3.5:9b | `agents/qa/inbox/` | 2 min | web search via DuckDuckGo (max 3 searches/task) |

## Orchestrator — 3-Phase Loop

Every minute the orchestrator runs three phases in sequence:

**Phase 1 — Validation.** Scans `validation/` for completed subtasks, groups them by `parent_task_id`, then calls the validation LLM (`validation_system_prompt.md`) for each parent. The LLM returns one of four decisions:

| Decision | Meaning | Action |
|---|---|---|
| `complete` | Work satisfies requirements | Move parent from `processing/` → `outbox/` |
| `refine` | Mostly good, minor improvements needed | Create follow-up subtasks, increment `iteration` |
| `additional_work` | Sound approach but incomplete | Create follow-up subtasks, increment `iteration` |
| `redo` | Does not meet requirements | Create new subtasks with failure context, increment `iteration` |

Maximum 5 iterations per parent task; forced `complete` at the limit to prevent infinite loops.

If a subtask in `validation/` references a parent task that no longer exists in `processing/`, the orchestrator checks `outbox/` before acting: if the parent is there with `status: complete` (e.g. force-completed before a scheduler restart), the subtask is moved to `outbox/` rather than `failed/`. Only if the parent is missing from both locations is the subtask moved to `failed/`.

**Phase 2 — Dependency resolution.** Scans all worker inboxes for tasks with a `depends_on` field. For each, checks whether the dependency's result file exists in `outbox/`. If all dependencies are resolved, it wires the result paths into `context_files` and removes `depends_on`, unblocking the task.

**Phase 3 — Dispatch.** Reads new parent tasks from `inbox/`, calls the decomposition LLM (`system_prompt.md`), creates subtasks in worker inboxes, and moves the parent to `processing/` with `status: dispatched`. The `dispatched` status prevents orphan recovery from re-queueing the parent on subsequent cycles. When research and coder subtasks are created together, the coder task is automatically given `depends_on: [research_task_id]` so it waits for the research output before running. Ollama timeout is set to 240s (`config.json`) to accommodate complex decomposition calls.

## Task Flow

```
inbox/          → orchestrator decomposes → agents/*/inbox/   (parent moves to processing/)
agents/*/inbox/ → worker executes         → validation/       (not outbox/)
validation/     → orchestrator validates  → outbox/ (complete) or back to agents/ (refine/redo)
```

Workers never write directly to `outbox/`. They write their result file to `outbox/` but move their task file to `validation/` via `mark_awaiting_validation()`. Only the orchestrator's `complete` decision moves the parent task to `outbox/`.

## Claude Code — Approval Gate

Tasks the orchestrator routes to `claude-code` go to `agents/claude-code/pending/` (not `inbox/`) until approved. This prevents unattended claude CLI invocations.

**Via dashboard:** the **Approvals** tab lists all pending tasks with Approve and Reject buttons. Approve moves the file to `agents/claude-code/inbox/`; Reject moves it to `failed/` with the rejection reason appended.

**Manually:** move the `.task.md` file from `agents/claude-code/pending/` to `agents/claude-code/inbox/`.

Once in `agents/claude-code/inbox/`, the agent picks it up on its next 3-minute poll.

## QA Loop

All code tasks automatically chain through QA:

1. Orchestrator sets `chain_to: qa` on every code subtask.
2. Coder completes → creates QA task in `agents/qa/inbox/` with the result file in `context_files`.
3. QA: extracts code → executes via subprocess (30s timeout) → reviews with qwen3.5:9b.  
   May perform up to 3 DuckDuckGo searches to look up errors or verify library usage.
4. **PASS** → writes approval to `outbox/`, moves task to `validation/`.
5. **FAIL, retry_count=0** → creates new coder task with QA feedback, `retry_count=1`.
6. **FAIL, retry_count=1** → writes failure report to `failed/`, moves task to `validation/`.

## Token Logging

After every successful Ollama call, each agent logs token usage to `logs/<agent>/tokens.jsonl`:

```json
{"ts": "2026-05-07T10:00:01Z", "task_id": "task_20260507_...", "prompt": 312, "completion": 87}
```

Helper: `scripts/shared/token_logger.py` — `log_tokens(agent_name, task_id, prompt_tokens, completion_tokens)`. The log is append-only; it accumulates across all runs. The dashboard **Agent Stats** tab reads these files and displays cumulative `Prompt Tokens`, `Completion Tokens`, and `LLM Calls` per agent. `claude-code` always shows `—` (no Ollama calls).

## Ollama API

Both modes use `scripts/shared/ollama_client.py`:

**Plain chat** (`OllamaClient.chat()`) — used by orchestrator, coder:
```
POST http://192.168.1.13:11434/api/chat
{ "model": "...", "messages": [...], "stream": false }
```

**Tool-calling loop** (`OllamaClient.chat_with_tools()`) — used by research and QA:
```
POST http://192.168.1.13:11434/api/chat
{ "model": "...", "messages": [...], "tools": [...], "stream": false }
```
Returns `{"type": "text", ...}` or `{"type": "tool_call", "name": "web_search", "arguments": {...}}`. The agent executes the search, appends results to message history, and loops until a text response is received or the turn limit is hit.

## Claude Code Worker

Every prompt is prefixed with `_PIPELINE_PREAMBLE` before being passed to the CLI:

```python
prompt = _PIPELINE_PREAMBLE + task_body
subprocess.run(["claude", "--print", "-p", prompt], capture_output=True, text=True)
```

The preamble instructs the agent to write its complete response as plain text in stdout — not to attempt filesystem writes, use tools, or request permissions. Without this, the CLI running non-interactively may emit a permission-request string instead of the actual output when it infers the task expects a file write.

## Concurrency

The orchestrator uses a lockfile (`processing/orchestrator.lock`) to prevent concurrent instances. Stale locks (dead PID) are cleaned up automatically on the next run.

**Orphan recovery.** At startup (before Phase 1), the orchestrator scans `processing/` for any `.task.md` file with `status: pending`. These are tasks that were moved to `processing/` by `mark_processing()` but whose decomposition never completed — typically because the process was killed mid-LLM-call. They are moved back to `inbox/` and logged as warnings so they re-enter the pipeline on the next cycle. Tasks with `status: processing` (intentionally placed there by the validation loop) are not touched.

When the validation phase encounters a subtask whose parent is no longer in `processing/`, it checks `outbox/` before marking the subtask as failed. If the parent is in `outbox/` with `status: complete` — as happens when a parent was force-completed just before a scheduler restart — the subtask is moved to `outbox/` instead. This prevents legitimate completed work from appearing as failures in the dashboard.

**SIGINT isolation.** Each agent subprocess is spawned with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows or `start_new_session=True` on Unix. This isolates agent processes from the scheduler's console signal group, so pressing Ctrl+C in the scheduler terminal stops the scheduler gracefully without propagating the signal to any in-flight agent LLM call.

## Configuration

All runtime settings in `config.json` at the project root, loaded via `scripts/shared/config.py`:

```json
{
  "ollama": { "base_url": "http://192.168.1.13:11434", "timeout": 240 },
  "agents": {
    "orchestrator": { "model": "qwen3.5:9b" },
    "coder":        { "model": "qwen2.5-coder:7b" },
    "research":     { "model": "qwen3.5:9b" },
    "qa":           { "model": "qwen3.5:9b" },
    "claude-code":  { "cli": true, "timeout": 300 }
  },
  "dashboard": { "port": 5000, "debug": false, "poll_interval": 1500 }
}
```

## Scheduler Startup Sequence

Before spawning any agent subprocesses, `scheduler.py run()` performs two safety checks:

1. **Flush `.pyc` caches** — recursively deletes all `__pycache__` directories under `scripts/` so agents always import fresh source bytecode. Prevents stale cached modules from masking syntax errors in recently edited files.

2. **Health-check import** — test-imports `scripts/shared/task_io.py` using `importlib.util`. If the import fails (syntax error, truncation, etc.) the scheduler logs a `FATAL` error and returns without starting any agents. This provides an early-warning gate against a class of crash that previously allowed one cached orchestrator cycle to succeed before all agents crashed simultaneously.

Both checks run after the Ollama availability check and before `_schedule_agents()`.

## QA Feedback

When QA issues a `FAIL` verdict it creates a retry coder task whose body includes the full `FEEDBACK:` block from the LLM review. The regex `r"FEEDBACK:\s*(.*)"` with `re.DOTALL` captures everything from the `FEEDBACK:` marker to the end of the response, preserving multi-line feedback. The coder receives the complete list of issues rather than a truncated first line.

## Dashboard

A separate Flask process, reads directly from the filesystem (no DB). Start independently:

```bash
python dashboard/run_dashboard.py           # http://localhost:5000
python dashboard/run_dashboard.py --port 8000 --debug
```

REST endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/status` | System metrics (pending/processing/completed/failed/awaiting_approval counts) |
| `GET /api/tasks` | All tasks; optional `?status=` and `?type=` filters |
| `GET /api/tasks/<id>` | Full task detail: metadata, body, logs, result |
| `GET /api/tasks/<id>/payload` | Raw task file content (frontmatter + body) |
| `GET /api/agents` | Per-agent stats: completed, errors, prompt_tokens, completion_tokens, llm_calls |
| `GET /api/agents/<name>/logs` | Last N log lines for an agent |
| `GET /api/pending-approvals` | Tasks waiting in `agents/claude-code/pending/` |
| `POST /api/pending-approvals/<id>/approve` | Move task to `agents/claude-code/inbox/` |
| `POST /api/pending-approvals/<id>/reject` | Move task to `failed/` with rejection reason |
| `POST /api/tasks/submit` | Create a task in `inbox/` directly from the dashboard |

See `DASHBOARD.md` for full API docs, configuration, and troubleshooting.

## Diagrams

- `ai-team-architecture.drawio` — System topology
- `ai-team-message-flows.drawio` — Message flow and QA loop
