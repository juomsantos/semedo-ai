# AI Team Agent Coordination System — Implementation Complete

## Status: Fully Functional (with QA Loop + Dashboard)

All agent scripts and the real-time web dashboard are implemented, tested end-to-end, and running.

## What Was Built

### Core Agents

| Agent | Script | Model | Role |
|---|---|---|---|
| Orchestrator | `agent_orchestrator.py` | qwen3.5:9b | Routes & decomposes tasks |
| Coder | `agent_coder.py` | qwen2.5-coder:7b | Code generation |
| Research | `agent_research.py` | qwen3.5:9b | Research, summarization |
| Claude Code | `agent_claude_code.py` | Claude CLI | Complex reasoning tasks |
| QA | `agent_qa.py` | qwen3.5:9b | Code review + execution testing |

### Shared Infrastructure

- `scripts/shared/task_io.py` — task file I/O (read/write/move, supports `chain_to`, `retry_count`, `original_description`)
- `scripts/shared/ollama_client.py` — Ollama REST wrapper (`http://192.168.1.13:11434`)
- `scripts/shared/logger.py` — file + stdout logging, UTF-8 safe on Windows
- `scripts/shared/config.py` — `ProjectConfig` class; loads `config.json` for Ollama URL, agent models, dashboard settings
- `scripts/scheduler.py` — cross-platform Python scheduler (replaces cron on Windows)
- `config.json` — centralized runtime config (Ollama URL, agent models, dashboard port/poll interval)
- `RUN_SCHEDULER.bat` — Windows quick-start (agents only)

### Dashboard

Real-time web monitoring UI at `http://localhost:5000`. Runs independently from the scheduler.

- `dashboard/app.py` — Flask REST API (status, tasks, agent stats, logs endpoints)
- `dashboard/task_monitor.py` — filesystem scanner; reads all task folders and log files
- `dashboard/run_dashboard.py` — CLI launcher; reads port/debug from `config.json`
- `dashboard/templates/index.html` — single-page UI (Active Tasks, History, Agent Stats, Logs tabs)
- `dashboard/static/dashboard.js` — JavaScript polling every 1.5 s
- `dashboard/static/dashboard.css` — styling

### QA Loop

Code tasks automatically flow through QA:
1. Coder completes → chains to `agents/qa/inbox/`
2. QA extracts code, runs it via subprocess (30s timeout), reviews with qwen3.5:9b
3. **PASS** → result written to `outbox/`
4. **FAIL (first attempt)** → new coder task with feedback, `retry_count=1`
5. **FAIL (retry)** → failure report written to `failed/`

### Reliability Fixes

- Orchestrator lockfile (`processing/orchestrator.lock`) prevents concurrent instances
- Stale lock auto-cleanup based on PID check
- UTF-8 encoding fix in logger for Windows compatibility

## Configuration

All settings in `config.json` at the project root, loaded by `scripts/shared/config.py`:

- **Ollama URL:** `http://192.168.1.13:11434`
- **Models:** `qwen3.5:9b` (orchestrator, research, QA), `qwen2.5-coder:7b` (coder)
- **Dashboard port:** `5000` (configurable in `config.json` under `dashboard.port`)

## Quick Start

**Terminal 1 — Agents:**
```bash
RUN_SCHEDULER.bat
```

Agents poll at their intervals (orchestrator 1 min, workers 2 min, claude-code 3 min).

**Terminal 2 — Dashboard:**
```bash
python dashboard/run_dashboard.py
```

Open `http://localhost:5000` in your browser.

## Submitting Tasks

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

## Monitoring

- **Dashboard:** `http://localhost:5000` — real-time task status, agent stats, live logs
- **Logs:** `logs/<agent>/general.log`
- **Scheduler log:** `logs/scheduler/general.log`
- **Results:** `outbox/`
- **Failures:** `failed/` (includes QA reports with execution output + feedback)

## Diagrams

- `ai-team-architecture.drawio` — Full system topology (João/Cowork → Orchestrator → Workers → QA → outbox/failed), color-coded by role, with Scheduler trigger lines
- `ai-team-message-flows.drawio` — Message flow flowchart: task type routing, code/QA happy path, retry on first fail, failure report on second fail

## Next Steps (Optional)

1. **Task dependencies** — parent-child task tracking for multi-step workflows
2. **Result aggregation** — summarize results from multiple agents for review
3. **Webhooks** — notify when tasks complete
4. **File watcher** — replace polling with `inotify`/`watchman` for lower latency
5. **RAG** — use embedding + rerank models for context-aware task routing
