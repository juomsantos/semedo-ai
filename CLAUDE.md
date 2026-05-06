# AI Team — Agent Coordination System

This project is a fully implemented multi-agent AI coordination system. Agents communicate through a shared filesystem, polled on a schedule via `scripts/scheduler.py`. See `ARCHITECTURE.md` for the full design and `IMPLEMENTATION_COMPLETE.md` for a summary of what was built.

## Current Status: Fully Implemented ✓

All agents are built, tested end-to-end, and running. The system is in maintenance/extension mode.

## What's Running

A three-tier multi-agent system:

1. **Claude (Cowork)** — master coordinator, writes tasks to `inbox/`
2. **Orchestrator** (`qwen3.5:9b`) — polls `inbox/` every 1 min, routes and decomposes tasks into subtasks, writes to worker inboxes
3. **Workers:**
   - `qwen2.5-coder:7b` (coder) — code generation, polls every 2 min
   - `qwen3.5:9b` (research) — research, summarization, Q&A, polls every 2 min
   - `claude CLI` (claude-code) — complex/reasoning tasks, polls every 3 min
   - `qwen3.5:9b` (qa) — code review + execution testing, polls every 2 min

## Key Technical Decisions

- **Ollama REST API** at `http://192.168.1.13:11434/api/chat`, `stream: false`
- **Claude Code worker:** `subprocess.run(["claude", "--print", "-p", task_content])`
- **Task files** are `.task.md` with YAML frontmatter (see `ARCHITECTURE.md` for schema)
- **System prompts** stored as files in `agents/<name>/system_prompt.md` — edit those to change agent behaviour without touching code
- **QA loop:** code tasks chain automatically through QA after the coder; one auto-retry on failure, failure report to `failed/` on second failure
- **Concurrency guard:** orchestrator uses a lockfile (`processing/orchestrator.lock`) with PID validation so concurrent cron/scheduler invocations don't double-process
- **Scheduler** is a Python threading-based loop (`scripts/scheduler.py`), not cron — works on Windows

## Folder Structure

```
AI Team/
  CLAUDE.md                    ← you are here
  ARCHITECTURE.md              ← full design doc
  IMPLEMENTATION_COMPLETE.md   ← what was built and how
  ai-team-architecture.drawio  ← system topology diagram
  ai-team-message-flows.drawio ← message flow / QA loop diagram
  RUN_SCHEDULER.bat            ← Windows quick-start
  requirements.txt
  inbox/                       ← drop .task.md files here to submit work
  processing/                  ← tasks in flight (+ orchestrator.lock)
  outbox/                      ← completed results
  failed/                      ← QA failure reports + errored tasks
  context/                     ← optional shared context files for tasks
  agents/
    orchestrator/system_prompt.md
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
  logs/                        ← per-agent logs at logs/<agent>/general.log
  scripts/
    shared/
      task_io.py               ← task file I/O helpers
      ollama_client.py         ← Ollama REST wrapper
      logger.py                ← UTF-8-safe logger
    agent_orchestrator.py
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py
```

## Running the System

**Windows (batch file):**
```
RUN_SCHEDULER.bat
```

**Manual:**
```bash
python scripts/scheduler.py
```

All 5 agents start on their intervals. Logs: `logs/scheduler/general.log` and `logs/<agent>/general.log`. Press Ctrl+C to stop.

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

- Results land in `outbox/`
- QA failures (with execution output + feedback) land in `failed/`
- Logs: `logs/<agent>/general.log`

## How to Resume / Next Steps

The system is complete and working. Potential extensions:

1. **Task dependencies** — parent-child task tracking for multi-step workflows
2. **Result aggregation** — summarize outputs from multiple agents
3. **Web dashboard** — real-time monitoring UI over the shared folder
4. **Webhooks** — notify when tasks complete
5. **File watcher** — replace polling with `inotify`/`watchman` for lower latency
6. **RAG** — use embedding + rerank models for context-aware task routing
