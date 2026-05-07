# AI Team — Agent Coordination System

This project is a multi-agent AI coordination system. Agents communicate through a shared filesystem, polled on a schedule via `scripts/scheduler.py`. A real-time web dashboard is available at `http://localhost:5000`. See `ARCHITECTURE.md` for the full design and `DASHBOARD.md` for dashboard usage.

## Current Status: Fully Implemented ✓

All agents are built and running with a continuous **orchestrator validation loop**: completed subtask results flow into `validation/`, the orchestrator reviews them, and decides whether to accept, refine, or request more work — up to 5 iterations before forcing completion.

## What's Running

A three-tier multi-agent system:

1. **Claude (Cowork)** — master coordinator, writes tasks to `inbox/`
2. **Orchestrator** (`qwen3.5:9b`) — polls `inbox/` every 3 min, runs 3 phases per cycle: validate completed work → resolve task dependencies → decompose and dispatch new tasks
3. **Workers:**
   - `qwen2.5-coder:7b` (coder) — code generation, polls every 2 min; skips tasks with unresolved dependencies
   - `qwen3.5:9b` (research) — research, summarization, Q&A, polls every 2 min; live web search via DuckDuckGo (up to 5 searches/task)
   - `claude CLI` (claude-code) — complex/reasoning tasks, polls every 3 min; tasks require manual approval first (land in `agents/claude-code/pending/` before `inbox/`)
   - `qwen3.5:9b` (qa) — code review + execution testing, polls every 2 min; live web search for error lookup (up to 3 searches/task)

4. **Dashboard** (`Flask`) — real-time web UI at `http://localhost:5000`; start with `python dashboard/run_dashboard.py`

## Key Technical Decisions

- **Ollama REST API** at `http://192.168.1.13:11434/api/chat`, `stream: false`
- **Tool-calling loop** (`chat_with_tools`) used by both research and QA agents for DuckDuckGo web search
- **Claude Code worker:** `subprocess.run(["claude", "--print", "-p", task_content])`
- **Task files** are `.task.md` with YAML frontmatter — see `ARCHITECTURE.md` for full schema
- **System prompts** stored in `agents/<name>/system_prompt.md` — edit to change agent behaviour without touching code. The orchestrator has two: `system_prompt.md` (decomposition) and `validation_system_prompt.md` (validation decisions)
- **Validation loop:** workers move completed tasks to `validation/` (not `outbox/`) via `mark_awaiting_validation()`; the orchestrator's Phase 1 reviews them and decides complete/refine/redo/additional_work. Max 5 iterations. The parent task stays in `processing/` throughout — it is only moved to `outbox/` when the orchestrator issues a `complete` decision.
- **Task dependencies:** coder tasks automatically get `depends_on: [research_task_id]` when research and code subtasks coexist; the orchestrator's Phase 2 wires the research result into `context_files` once complete, then unblocks the coder task
- **Orphan recovery:** on every startup, the orchestrator scans `processing/` for tasks with `status: pending` (tasks it started decomposing but never finished, e.g. killed mid-LLM-call) and moves them back to `inbox/` to be re-dispatched. Tasks with `status: processing` (placed there by the validation loop) are left untouched.
- **SIGINT isolation:** agent subprocesses are spawned with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` (Windows) or `start_new_session=True` (Unix) so a Ctrl+C in the scheduler terminal does not propagate to agents mid-LLM-call, preventing task orphaning.
- **Startup health checks:** before spawning any agents, the scheduler (a) flushes all `__pycache__` directories under `scripts/` so agents always import fresh bytecode, and (b) test-imports `shared/task_io.py` — if it fails to import, the scheduler logs a FATAL error and aborts without starting any agents.
- **QA feedback:** the `FEEDBACK:` block in QA's LLM response is captured in full (multi-line) using `re.DOTALL`, so retry tasks receive complete actionable feedback rather than a truncated first line.
- **Task ID uniqueness:** IDs include microseconds (`task_YYYYMMDD_HHMMSS_microseconds`) to prevent collisions when subtasks are created in the same second
- **Approval gate for claude-code:** orchestrator routes to `pending_approval` which places tasks in `agents/claude-code/pending/`; approve or reject from the dashboard **Approvals** tab (or manually move files)
- **Token logging:** after every Ollama call, each agent appends `{ts, task_id, prompt, completion}` to `logs/<agent>/tokens.jsonl` via `scripts/shared/token_logger.py`; the dashboard Agent Stats tab shows cumulative totals
- **Concurrency guard:** orchestrator uses a lockfile (`processing/orchestrator.lock`) with PID validation
- **Scheduler** is a Python threading-based loop (`scripts/scheduler.py`), not cron — works on Windows
- **Config** centralized in `config.json`; loaded via `scripts/shared/config.py` (`ProjectConfig` class)
- **Dashboard** is a separate Flask process (`dashboard/app.py`); reads directly from the shared filesystem — no DB required; tasks can also be submitted directly from the **Submit Task** tab
- **Log timestamps** use `datetime.fromtimestamp(time.time(), tz=timezone.utc)` — correct UTC on Windows

## Folder Structure

```
AI Team/
  CLAUDE.md                              ← you are here
  ARCHITECTURE.md                        ← full design doc
  DASHBOARD.md                           ← dashboard usage and API reference
  config.json                            ← centralized config
  RUN_SCHEDULER.bat / RUN_SCHEDULER.sh   ← quick-start scripts
  requirements.txt
  inbox/                   ← drop .task.md files here to submit work
  processing/              ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/              ← completed subtasks awaiting orchestrator approval
  outbox/                  ← approved & completed results
  failed/                  ← QA failure reports + hard-errored tasks
  context/                 ← optional shared context files for tasks
  agents/
    orchestrator/
      system_prompt.md             ← decomposition & routing prompt
      validation_system_prompt.md  ← validation decision prompt
    coder/
      inbox/
      system_prompt.md
    research/
      inbox/
      system_prompt.md
    claude-code/
      inbox/              ← approved tasks (ready to run)
      pending/            ← tasks awaiting manual approval
    qa/
      inbox/
      system_prompt.md
  dashboard/
    app.py / run_dashboard.py / task_monitor.py
    templates/index.html
    static/dashboard.js / dashboard.css
  logs/                   ← per-agent logs at logs/<agent>/general.log
  scripts/
    shared/
      task_io.py          ← task file I/O, dependency resolution, validation grouping
      ollama_client.py    ← Ollama REST wrapper (chat + chat_with_tools); stores last_token_counts
      token_logger.py     ← appends per-call token usage to logs/<agent>/tokens.jsonl
      web_search.py       ← DuckDuckGo search wrapper
      logger.py           ← UTC-correct logger
      config.py           ← config.json loader
    agent_orchestrator.py
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py
```

## Running the System

**Terminal 1 — Agents:**
```
RUN_SCHEDULER.bat        (Windows)
RUN_SCHEDULER.sh         (Linux/Mac)
```
Or manually: `python scripts/scheduler.py`

Logs: `logs/scheduler/general.log` and `logs/<agent>/general.log`. Press Ctrl+C to stop.

**Terminal 2 — Dashboard (optional):**
```bash
python dashboard/run_dashboard.py
```
Open `http://localhost:5000`. Runs independently of the scheduler.

## Submitting a Task

**Easiest:** use the dashboard **Submit Task** tab at `http://localhost:5000` — fill in type, priority, description, and optional expected output, then click Submit.

**Programmatically:** drop a `.task.md` file in `inbox/`:

```markdown
---
id: task_20260507_100000_000000
type: code
priority: medium
created_by: claude-cowork
created_at: 2026-05-07T10:00:00
assigned_to: orchestrator
status: pending
output_path: outbox/task_20260507_100000_000000_result.md
context_files: []
---

## Task Description
Write a Python function that ...

## Expected Output
A working Python file with ...
```

The orchestrator picks it up within 1 minute, decomposes it, and routes subtasks to workers.

## Monitoring

- **Dashboard:** `http://localhost:5000` — real-time task status, agent stats, live logs, approve/reject claude-code tasks
- **Task flow:** `inbox/` → `processing/` → workers → `validation/` → `outbox/` (or `failed/`)
- **Logs:** `logs/<agent>/general.log` | token usage: `logs/<agent>/tokens.jsonl`
- **Pending claude-code tasks:** appear in the dashboard **Approvals** tab with Approve / Reject buttons; or manually move from `agents/claude-code/pending/` to `agents/claude-code/inbox/`

## Potential Extensions

1. **Parent-child UI** — dashboard currently shows a flat task list; hierarchy view would help track validation iterations
2. **Worker-initiated research** — allow coder/QA to drop tasks in `research/inbox/` mid-execution and yield until resolved
3. **Webhooks** — notify when tasks complete
4. **File watcher** — replace polling with `inotify`/`watchman` for lower latency
5. **RAG** — embedding + rerank for context-aware routing
