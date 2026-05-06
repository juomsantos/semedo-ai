# AI Team — Multi-Agent Architecture

> Last updated: 2026-05-06

## Overview

A team of agents coordinated through this shared folder. Agents poll their inboxes on a schedule (via `scheduler.py`) and communicate exclusively through structured task files. Ollama runs at `http://192.168.1.13:11434`.

## Topology

```
[João / Claude (Cowork)]
        │  writes tasks
        ▼
   inbox/
        │
        ▼  polls every 1 min
[Orchestrator: qwen3.5:9b]
   - Routes tasks to correct worker
   - Decomposes complex tasks into subtasks
   - Sets chain_to: qa for all code tasks
   - Writes to agents/*/inbox/
        │
   ┌────┼────────────────┐
   ▼    ▼                ▼
[Coder]  [Research]   [Claude Code]
qwen2.5  qwen3.5:9b    claude CLI
coder:7b
   │ chain_to: qa
   ▼
[QA Agent: qwen3.5:9b]
   - Extracts code from result
   - Executes via subprocess (30s timeout)
   - Reviews with qwen3.5:9b
   - PASS → outbox/
   - FAIL (retry_count=0) → creates retry coder task
   - FAIL (retry_count=1) → writes report to failed/
        │
   ┌────┴────────┐
   ▼             ▼
outbox/       failed/
        │
        ▼
[Claude (Cowork) reviews & delivers to João]
```

## Folder Structure

```
AI Team/
  ARCHITECTURE.md          ← this file
  CLAUDE.md                ← project instructions for Claude Code
  IMPLEMENTATION_COMPLETE.md
  QA_AGENT_BRIEFING.md
  inbox/                   ← drop task files here to start work
  processing/              ← tasks currently being handled (+ orchestrator.lock)
  outbox/                  ← completed results (task files + result files)
  failed/                  ← tasks that errored (with QA failure reports)
  agents/
    orchestrator/
      system_prompt.md     ← routing & decomposition instructions
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
  logs/                    ← per-agent execution traces
  scripts/                 ← agent Python scripts
    shared/
      task_io.py           ← task file read/write/move helpers
      ollama_client.py     ← Ollama REST wrapper
      logger.py            ← file + stdout logging
    agent_orchestrator.py
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py           ← background polling scheduler
  RUN_SCHEDULER.bat        ← Windows quick-start
```

## Task File Format

```markdown
---
id: task_YYYYMMDD_HHMMSS
type: research|code|summarize|review|plan|qa
priority: high|medium|low
created_by: claude-cowork|orchestrator|coder|qa
created_at: 2026-05-06T10:00:00
assigned_to: orchestrator|coder|research|claude-code|qa
status: pending
output_path: outbox/task_YYYYMMDD_HHMMSS_result.md
context_files: []
chain_to: qa              ← optional: agent to chain to after completion
retry_count: 0            ← optional: number of QA retries attempted
original_description: ... ← optional: preserved across retries
---

## Task Description
...

## Expected Output
...
```

## Agent Scripts

All scripts live in `scripts/`. Each is standalone and invoked by the scheduler.

| Script | Model | Inbox | Interval |
|---|---|---|---|
| `agent_orchestrator.py` | qwen3.5:9b | `inbox/` | 1 min |
| `agent_coder.py` | qwen2.5-coder:7b | `agents/coder/inbox/` | 2 min |
| `agent_research.py` | qwen3.5:9b | `agents/research/inbox/` | 2 min |
| `agent_claude_code.py` | Claude Code CLI | `agents/claude-code/inbox/` | 3 min |
| `agent_qa.py` | qwen3.5:9b | `agents/qa/inbox/` | 2 min |

## Ollama API

```
POST http://192.168.1.13:11434/api/chat
{
  "model": "qwen3.5:9b",
  "messages": [
    {"role": "system", "content": "<system_prompt>"},
    {"role": "user", "content": "<task content>"}
  ],
  "stream": false
}
```

## Claude Code Worker

```python
subprocess.run(["claude", "--print", "-p", task_content], capture_output=True, text=True)
```

## QA Loop

All code tasks automatically chain through the QA agent:

1. Orchestrator sets `chain_to: qa` on every code subtask
2. Coder completes task → creates QA task in `agents/qa/inbox/` with result file as `context_files`
3. QA agent: extracts code → executes via subprocess → reviews with qwen3.5:9b
4. **PASS** → writes approval to `outbox/`
5. **FAIL, retry_count=0** → creates new coder task with QA feedback, `retry_count=1`
6. **FAIL, retry_count=1** → writes failure report to `failed/`

## Concurrency

The orchestrator uses a lockfile (`processing/orchestrator.lock`) to prevent concurrent instances. Stale locks (dead PID) are cleaned up automatically on next run.

## IDE Agents (Passive)

**Copilot** and **Continue.dev** consume the shared folder as context. Not active polling workers.

## Diagrams

- `ai-team-architecture.drawio` — Full system topology (agents, inboxes, outbox, failed, scheduler)
- `ai-team-message-flows.drawio` — Message flow flowchart (task routing, QA loop, retry/fail paths)
