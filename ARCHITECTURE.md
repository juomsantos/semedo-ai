# AI Team — Multi-Agent Architecture

> Last updated: 2026-05-07

## Overview

A team of agents coordinated through this shared folder. Agents poll their inboxes on a schedule (via `scheduler.py`) and communicate exclusively through structured task files. Ollama runs at `http://192.168.1.13:11434`.

## Topology

```
[João / Claude (Cowork)]
        │  writes tasks
        ▼
   inbox/
        │
        ▼  polls every 1 min — 3 phases per run
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
      ollama_client.py         ← Ollama REST wrapper: chat() and chat_with_tools()
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
status: pending
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

## Agent Scripts

All scripts live in `scripts/`. Each is standalone and invoked by the scheduler.

| Script | Model | Inbox | Interval | Notes |
|---|---|---|---|---|
| `agent_orchestrator.py` | qwen3.5:9b | `inbox/` | 1 min | 3-phase loop per run |
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

**Phase 2 — Dependency resolution.** Scans all worker inboxes for tasks with a `depends_on` field. For each, checks whether the dependency's result file exists in `outbox/`. If all dependencies are resolved, it wires the result paths into `context_files` and removes `depends_on`, unblocking the task.

**Phase 3 — Dispatch.** Reads new parent tasks from `inbox/`, calls the decomposition LLM (`system_prompt.md`), creates subtasks in worker inboxes, and moves the parent to `processing/`. When research and coder subtasks are created together, the coder task is automatically given `depends_on: [research_task_id]` so it waits for the research output before running.

## Task Flow

```
inbox/          → orchestrator decomposes → agents/*/inbox/   (parent moves to processing/)
agents/*/inbox/ → worker executes         → validation/       (not outbox/)
validation/     → orchestrator validates  → outbox/ (complete) or back to agents/ (refine/redo)
```

Workers never write directly to `outbox/`. They write their result file to `outbox/` but move their task file to `validation/` via `mark_awaiting_validation()`. Only the orchestrator's `complete` decision moves the parent task to `outbox/`.

## Claude Code — Approval Gate

Tasks the orchestrator routes to `claude-code` go to `agents/claude-code/pending/` (not `inbox/`) until João manually moves or approves them. This prevents unattended claude CLI invocations. Once moved to `agents/claude-code/inbox/`, the agent picks them up on its next 3-minute poll.

## QA Loop

All code tasks automatically chain through QA:

1. Orchestrator sets `chain_to: qa` on every code subtask.
2. Coder completes → creates QA task in `agents/qa/inbox/` with the result file in `context_files`.
3. QA: extracts code → executes via subprocess (30s timeout) → reviews with qwen3.5:9b.  
   May perform up to 3 DuckDuckGo searches to look up errors or verify library usage.
4. **PASS** → writes approval to `outbox/`, moves task to `validation/`.
5. **FAIL, retry_count=0** → creates new coder task with QA feedback, `retry_count=1`.
6. **FAIL, retry_count=1** → writes failure report to `failed/`, moves task to `validation/`.

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

```python
subprocess.run(["claude", "--print", "-p", task_content], capture_output=True, text=True)
```

## Concurrency

The orchestrator uses a lockfile (`processing/orchestrator.lock`) to prevent concurrent instances. Stale locks (dead PID) are cleaned up automatically on the next run.

## Configuration

All runtime settings in `config.json` at the project root, loaded via `scripts/shared/config.py`:

```json
{
  "ollama": { "base_url": "http://192.168.1.13:11434", "timeout": 120 },
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

## Dashboard

A separate Flask process, reads directly from the filesystem (no DB). Start independently:

```bash
python dashboard/run_dashboard.py           # http://localhost:5000
python dashboard/run_dashboard.py --port 8000 --debug
```

REST endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/status` | System metrics (pending/processing/completed/failed counts, agent stats) |
| `GET /api/tasks` | All tasks; optional `?status=` and `?type=` filters |
| `GET /api/tasks/<id>` | Full task detail with logs and result preview |
| `GET /api/agents` | Per-agent completion and error counts |
| `GET /api/agents/<name>/logs` | Last N log lines for an agent |

See `DASHBOARD.md` for full API docs, configuration, and troubleshooting.

## Diagrams

- `ai-team-architecture.drawio` — System topology
- `ai-team-message-flows.drawio` — Message flow and QA loop
