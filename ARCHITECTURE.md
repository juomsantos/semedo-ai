# AI Team — Multi-Agent Architecture

> Last updated: 2026-05-05

## Overview

A team of agents coordinated through this shared folder. Agents poll their inboxes on a cron schedule and communicate exclusively through structured task files.

## Topology

```
[João / Claude (Cowork)]
        │  writes tasks
        ▼
   inbox/  ◄──────────────────────────────────────────┐
        │                                              │
        ▼  polls every 1 min                          │ escalates
[Orchestrator: qwen3:9b]                              │
   - Routes tasks to correct worker                   │
   - Decomposes complex tasks into subtasks           │
   - Writes to agents/*/inbox/                        │
        │                                      [Claude Code CLI]
   ┌────┼────────────────┐                           worker for
   ▼    ▼                ▼                        complex tasks
[Coder]  [Research]   [Claude Code]
qwen2.5  qwen3:9b      claude CLI
coder:7b               subprocess
   │         │               │
   └────┬────┘               │
        ▼                    │
     outbox/  ◄──────────────┘
        │
        ▼
[Claude (Cowork) reviews & delivers to João]
```

## Folder Structure

```
AI Team/
  ARCHITECTURE.md          ← this file
  inbox/                   ← drop task files here to start work
  processing/              ← tasks currently being handled
  outbox/                  ← completed results
  failed/                  ← tasks that errored (with logs)
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
  logs/                    ← per-task execution traces
  scripts/                 ← agent Python scripts + cron setup
```

## Task File Format

Create a `.task.md` file in `inbox/` to start work:

```markdown
---
id: task_YYYYMMDD_NNN
type: research|code|summarize|review|plan
priority: high|medium|low
created_by: claude-cowork
created_at: 2026-05-05T10:00:00
assigned_to: orchestrator
status: pending
output_path: outbox/task_YYYYMMDD_NNN_result.md
context_files: []
---

## Task Description

What needs to be done.

## Expected Output

What the result should look like.
```

## Agent Scripts

All scripts live in `scripts/`. Each is standalone and cron-invoked.

| Script | Model | Inbox | Cron |
|---|---|---|---|
| `agent_orchestrator.py` | qwen3:9b | `inbox/` | `*/1 * * * *` |
| `agent_coder.py` | qwen2.5-coder:7b | `agents/coder/inbox/` | `*/2 * * * *` |
| `agent_research.py` | qwen3:9b | `agents/research/inbox/` | `*/2 * * * *` |
| `agent_claude_code.py` | Claude Code CLI | `agents/claude-code/inbox/` | `*/3 * * * *` |

## Ollama API (used by all local agents)

```
POST http://localhost:11434/api/chat
{
  "model": "qwen3:9b",
  "messages": [
    {"role": "system", "content": "<system_prompt>"},
    {"role": "user", "content": "<task content>"}
  ],
  "stream": false
}
```

## Claude Code Worker

```python
subprocess.run(
    ["claude", "--print", "-p", task_content],
    capture_output=True, text=True
)
```

## IDE Agents (Passive)

**Copilot** and **Continue.dev** are not active polling workers. They consume the shared folder as context — useful for a developer working in the IDE to have visibility into what the agent team has produced.
