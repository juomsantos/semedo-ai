# AI Team — Agent Coordination System

This project is a fully implemented multi-agent AI coordination system. Agents communicate through a shared filesystem, polled on a schedule via `scripts/scheduler.py`. A real-time web dashboard is available at `http://localhost:5000`. See `ARCHITECTURE.md` for the full design, `IMPLEMENTATION_COMPLETE.md` for a summary of what was built, and `DASHBOARD.md` for dashboard usage.

## Current Status: Fully Implemented with Validation Loop ✓

All agents are built, tested end-to-end, and running with **orchestrator validation loop** enabled. The orchestrator now continuously validates all completed work and decides whether to accept, refine, or request additional work until tasks meet requirements.

**New:** See [ORCHESTRATOR_VALIDATION_LOOP.md](ORCHESTRATOR_VALIDATION_LOOP.md) for validation architecture, and [RESEARCH_CODER_HANDOFF.md](RESEARCH_CODER_HANDOFF.md) for task dependency wiring.

## What's Running

A three-tier multi-agent system:

1. **Claude (Cowork)** — master coordinator, writes tasks to `inbox/`
2. **Orchestrator** (`qwen3.5:9b`) — polls `inbox/` every 1 min, routes and decomposes tasks into subtasks, writes to worker inboxes
3. **Workers:**
   - `qwen2.5-coder:7b` (coder) — code generation, polls every 2 min
   - `qwen3.5:9b` (research) — research, summarization, Q&A, polls every 2 min; has live web search via DuckDuckGo (model decides when to search, up to 5 searches per task)
   - `claude CLI` (claude-code) — complex/reasoning tasks, polls every 3 min
   - `qwen3.5:9b` (qa) — code review + execution testing, polls every 2 min

4. **Dashboard** (`Flask`) — real-time web UI at `http://localhost:5000`; polls agents, inbox, outbox, logs every 1.5s; start with `python dashboard/run_dashboard.py`

## Key Technical Decisions

- **Ollama REST API** at `http://192.168.1.13:11434/api/chat`, `stream: false`; research agent uses the tool-calling variant (`chat_with_tools`) for web search
- **Claude Code worker:** `subprocess.run(["claude", "--print", "-p", task_content])`
- **Task files** are `.task.md` with YAML frontmatter (see `ARCHITECTURE.md` for schema)
- **System prompts** stored as files in `agents/<name>/system_prompt.md` — edit those to change agent behaviour without touching code
- **QA loop:** code tasks chain automatically through QA after the coder; one auto-retry on failure, failure report to `failed/` on second failure
- **Concurrency guard:** orchestrator uses a lockfile (`processing/orchestrator.lock`) with PID validation so concurrent cron/scheduler invocations don't double-process
- **Scheduler** is a Python threading-based loop (`scripts/scheduler.py`), not cron — works on Windows
- **Config** centralized in `config.json` (Ollama URL, agent models, dashboard port); loaded via `scripts/shared/config.py`
- **Dashboard** is a separate Flask process (`dashboard/app.py`); reads directly from the shared filesystem — no DB required

## Folder Structure

```
AI Team/
  CLAUDE.md                           ← you are here
  ARCHITECTURE.md                     ← full design doc
  IMPLEMENTATION_COMPLETE.md          ← what was built and how
  DASHBOARD.md                        ← dashboard usage and API reference
  ORCHESTRATOR_VALIDATION_LOOP.md     ← validation architecture & workflow
  RESEARCH_CODER_HANDOFF.md           ← task dependencies & context passing
  ai-team-architecture.drawio         ← system topology diagram
  ai-team-message-flows.drawio        ← message flow / QA loop diagram
  config.json                         ← centralized config (Ollama URL, models, dashboard port)
  RUN_SCHEDULER.bat                   ← Windows quick-start (agents only)
  requirements.txt
  inbox/                              ← drop .task.md files here to submit work
  processing/                         ← parent tasks in validation loop (+ orchestrator.lock)
  validation/                         ← completed subtasks awaiting orchestrator validation
  outbox/                             ← approved & completed results
  failed/                             ← QA failure reports + errored tasks
  context/                            ← optional shared context files for tasks
  agents/
    orchestrator/
      system_prompt.md                ← decomposition prompt
      validation_system_prompt.md     ← validation decision prompt
    coder/
      inbox/
      system_prompt.md
    research/
      inbox/
      system_prompt.md
    claude-code/
      inbox/
    qa/
      inbox/
      system_prompt.md
  dashboard/                          ← real-time web monitoring UI
    app.py                            ← Flask REST API server
    run_dashboard.py                  ← launcher (reads config.json)
    task_monitor.py                   ← filesystem scanner
    templates/index.html              ← dashboard UI
    static/dashboard.js               ← frontend polling logic
    static/dashboard.css              ← styling
    README.md                         ← dashboard-specific docs
  logs/                               ← per-agent logs at logs/<agent>/general.log
  scripts/
    shared/
      task_io.py                      ← task file I/O helpers
      ollama_client.py                ← Ollama REST wrapper (chat + chat_with_tools)
      web_search.py                   ← DuckDuckGo search wrapper (used by research agent)
      logger.py                       ← UTF-8-safe logger (now with correct UTC timestamps)
      config.py                       ← config.json loader (ProjectConfig class)
    agent_orchestrator.py             ← now with validation loop (3 phases)
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py
```

## Running the System

**Terminal 1 — Agents (Windows batch file):**
```
RUN_SCHEDULER.bat
```

Or manually:
```bash
python scripts/scheduler.py
```

All 5 agents start on their intervals. Logs: `logs/scheduler/general.log` and `logs/<agent>/general.log`. Press Ctrl+C to stop.

**Terminal 2 — Dashboard (optional, run independently):**
```bash
python dashboard/run_dashboard.py
```

Open `http://localhost:5000` in your browser. Port and other settings are in `config.json` under `dashboard`. The dashboard can run independently of the scheduler.

## Submitting a Task

Drop a `.task.md` file in `inbox/`:

```markdown
---
id: task_20260506_001
type: code
priority: medium
created_by: claude-cowork
created_at: 2026-05-06T10:00:00
assigned_to: orchestrator
status: pending
output_path: outbox/task_20260506_001_result.md
context_files: []
---

## Task Description
Write a Python function that ...

## Expected Output
A working Python file with ...
```

The orchestrator picks it up within 1 minute and routes it to the right worker.

## Monitoring

- **Dashboard:** `http://localhost:5000` — real-time task status, agent stats, live logs (start with `python dashboard/run_dashboard.py`)
- Results land in `outbox/`
- QA failures (with execution output + feedback) land in `failed/`
- Logs: `logs/<agent>/general.log`

## How to Resume / Next Steps

The system is complete and working. Potential extensions:

1. **Task dependencies** — parent-child task tracking for multi-step workflows
2. **Result aggregation** — summarize outputs from multiple agents
3. **Webhooks** — notify when tasks complete
4. **File watcher** — replace polling with `inotify`/`watchman` for lower latency
5. **RAG** — use embedding + rerank models for context-aware task routing
6. **Web search for other agents** — extend `chat_with_tools()` loop to coder or QA if useful
