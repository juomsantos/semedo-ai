# AI Team Agent Coordination System — Implementation Complete

## Status: Fully Functional (with QA Loop)

All agent scripts are implemented, tested end-to-end, and running.

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
- `scripts/scheduler.py` — cross-platform Python scheduler (replaces cron on Windows)
- `RUN_SCHEDULER.bat` — Windows quick-start

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

- **Ollama URL:** `http://192.168.1.13:11434`
- **Models:** `qwen3.5:9b` (orchestrator, research, QA), `qwen2.5-coder:7b` (coder)

## Quick Start

```bash
RUN_SCHEDULER.bat
```

Agents poll at their intervals (orchestrator 1min, workers 2min, claude-code 3min).

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
3. **Web dashboard** — real-time monitoring UI over the shared folder
4. **Webhooks** — notify when tasks complete
5. **File watcher** — replace polling with `inotify`/`watchman` for lower latency
6. **RAG** — use embedding + rerank models for context-aware task routing
