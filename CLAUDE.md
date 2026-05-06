# AI Team — Agent Coordination System

This project builds a team of AI agents that coordinate through a shared filesystem. Agents poll their inboxes on a cron schedule. See `ARCHITECTURE.md` for the full design.

## Current Status: Implementation Phase

Architecture is finalized. Next step is building the scripts.

## What We're Building

A two-tier multi-agent system:

1. **Claude (Cowork)** — master coordinator, writes tasks to `inbox/`
2. **qwen3:9b via Ollama** — orchestrator, routes AND decomposes tasks into subtasks
3. **Workers:** `qwen2.5-coder:7b` (code), `qwen3:9b` (research/summarize), `Claude Code CLI` (complex tasks)

Agents are invoked by **cron**, not file watchers. Each agent is a self-contained Python script.

## Build Order

1. `scripts/shared/task_io.py` — task file read/write/move helpers ✓
2. `scripts/shared/ollama_client.py` — thin wrapper for `http://192.168.1.13:11434/api/chat` ✓
3. `scripts/agent_orchestrator.py` + `agents/orchestrator/system_prompt.md` ✓
4. `scripts/agent_coder.py` + `agents/coder/system_prompt.md` ✓
5. `scripts/agent_research.py` + `agents/research/system_prompt.md` ✓
6. `scripts/agent_claude_code.py` — wraps `claude --print -p ...` ✓
7. `scripts/scheduler.py` + `RUN_SCHEDULER.bat` — cross-platform Python scheduler ✓
8. End-to-end test: drop a task in `inbox/`, watch it flow through ✓
9. **[NEXT] QA agent** — see `QA_AGENT_BRIEFING.md` for full spec

## Key Technical Decisions

- Ollama REST API at `http://localhost:11434/api/chat`, `stream: false`
- Claude Code worker: `subprocess.run(["claude", "--print", "-p", task_content])`
- Task files are `.task.md` with YAML frontmatter (see ARCHITECTURE.md for schema)
- System prompts stored as files in `agents/<name>/system_prompt.md` (editable without touching code)
- Each script is idempotent — safe to run on cron even if inbox is empty

## Local Models (João's Ollama)

- `qwen3:9b` (or qwen3.5:9b) — orchestrator + research agent
- `qwen2.5-coder:7b` — coder agent
- Embedding/rerank models available for future RAG use

## Folder Structure

```
AI Team/
  CLAUDE.md          ← you are here
  ARCHITECTURE.md    ← full design doc
  inbox/
  processing/
  outbox/
  failed/
  agents/
    orchestrator/system_prompt.md
    coder/inbox/ + system_prompt.md
    research/inbox/ + system_prompt.md
    claude-code/inbox/
  logs/
  scripts/
    shared/
      task_io.py
      ollama_client.py
      logger.py
    agent_orchestrator.py
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    setup_cron.sh
```

## Running the System

### Start the Scheduler

The agent system polls on a schedule. Start the scheduler with:

**Windows (batch file):**
```bash
RUN_SCHEDULER.bat
```

**Windows (manual):**
```bash
python scripts/scheduler.py
```

**Linux/Mac (manual):**
```bash
python3 scripts/scheduler.py
```

The scheduler will start all 4 agents on their intervals:
- Orchestrator: every 1 minute
- Coder: every 2 minutes
- Research: every 2 minutes
- Claude Code: every 3 minutes

Logs appear in `logs/scheduler/general.log` and per-agent logs in `logs/<agent>/general.log`

Press Ctrl+C to gracefully stop.

## How to Resume

Start Claude Code in this folder (`cd` to this directory, then run `claude`). Read this file and `ARCHITECTURE.md`. The build order above tells you where to pick up.

If resuming for the QA agent task specifically, read `QA_AGENT_BRIEFING.md` — it contains the complete implementation spec.
