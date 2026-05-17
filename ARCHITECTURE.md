# AI Team — Solution Architecture

> Last updated: 2026-05-17

## Overview

AI Team is a self-contained, multi-agent AI coordination system that runs entirely on your local machine. Users interact with it through a real-time web dashboard; tasks can also be submitted programmatically by dropping files into the `inbox/` folder or via the dashboard REST API. No external coordinator is required — the system is fully autonomous once started.

A team of specialised AI agents collaborate through a shared filesystem. An event-driven file watcher triggers agents immediately when work arrives; a timer-based polling fallback is optionally available. The Orchestrator decomposes tasks, routes subtasks to workers, validates results, and iterates until quality criteria are met. Results accumulate in `outbox/` and are always accessible from the dashboard.

**Ollama server:** `http://192.168.1.13:11434` (configurable in `config.json`)  
**Dashboard:** `http://localhost:5000`  
**RAG API (knowledge base):** `http://localhost:8000`

---

## System Topology

```
╔══════════════════════════════════════════════════╗
║              Users & Clients                     ║
║  Dashboard UI  │  REST API  │  Direct file drop  ║
╚══════════════════════════════════════════════════╝
                        │  submits tasks
                        ▼
                     inbox/
                        │
                        │  file watcher triggers immediately
                        │  (timer fallback: optional, 0.5 min)
                        ▼
          ┌─────────────────────────────┐
          │   Orchestrator: qwen3.5:9b  │ ←── RAG pre-prompt injection
          │                             │
          │  Phase 1 — VALIDATION       │
          │  Phase 2 — DEPENDENCY RES.  │
          │  Phase 3 — DISPATCH         │
          └─────────────────────────────┘
                        │
          ┌─────────────┼──────────────────┐
          ▼             ▼                  ▼
      [Coder]      [Research]       [Claude Code*]
     qwen3.5:9b   qwen3.5:9b        claude CLI
     RAG inject   web_search        *requires approval
                  web_fetch         via dashboard
                  rag_query
          │
          │ chain_to: qa
          ▼
      [QA Agent: qwen3.5:9b]
      web_search + web_fetch + rag_query
      Executes code (30s subprocess timeout)
      PASS → validation/
      FAIL (1st attempt) → retry coder task with feedback
      FAIL (2nd attempt) → failed/
          │
          ▼
      validation/  ←── all workers land here
          │
          ▼  Orchestrator Phase 1
      complete | refine | redo | additional_work
          │              │
          ▼              └──► back to worker inboxes
       outbox/                (max 5 iterations)
          │
          ▼
    Results available in dashboard → Results tab

                   ┌──────────────────────────────────────┐
                   │    RAG API  (FastAPI + ChromaDB)      │
                   │    http://localhost:8000              │
                   │    Embedding: qwen3-embedding:8b      │
                   │    Reranking: cosine similarity       │
                   │    Started & monitored by scheduler   │
                   └──────────────────────────────────────┘
                          ↑  rag_query() calls from all agents
                          ↑  ingest via dashboard Knowledge Base tab
                             or POST /ingest
```

---

## How Users Interact with the System

The system is fully self-contained. No external AI assistant is needed to operate it.

| Interface | How to use |
|---|---|
| **Dashboard — Submit Task tab** | Fill in task type, priority, description, and expected output. Click Submit. |
| **Dashboard — Chat tab** | Converse with the pipeline assistant; it can create tasks on your behalf using `<CREATE_TASK>` blocks. |
| **File drop** | Write a `.task.md` file (see format below) and place it in `inbox/`. |
| **REST API** | `POST /api/tasks/submit` from any HTTP client or script. |
| **External clients** | Any tool that can write files or call the REST API — including AI assistants, scripts, or automation workflows — can submit and monitor tasks. |

Results are always available from the **Dashboard → Results tab** or by reading `outbox/*_result.md` files directly.

---

## Folder Structure

```
AI Team/
  ARCHITECTURE.md              ← this file
  CLAUDE.md                    ← system reference doc (tech decisions, folder guide)
  DASHBOARD.md                 ← dashboard usage and REST API reference
  config.json                  ← runtime config (Ollama URL, models, ports, API keys)
  RUN_SCHEDULER.bat            ← Windows quick-start (agents + RAG API)
  RUN_SCHEDULER.sh             ← Linux/Mac quick-start
  requirements.txt
  inbox/                       ← drop task files here to start work
  processing/                  ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/                  ← completed subtasks awaiting orchestrator approval
  outbox/                      ← approved & completed results
  failed/                      ← QA failure reports + hard-errored tasks
  context/                     ← optional shared context files for tasks
  rag_api/                     ← local knowledge base service
    main.py                    ← FastAPI app: /health /ingest /query /documents /documents/<id>
    config.py                  ← Settings (plain class, os.getenv; NOT pydantic_settings)
    ollama_client.py           ← embed() via /api/embeddings; rerank() via cosine similarity
    vector_store.py            ← ChromaDBPersistentClient; get() flat lists, query() nested lists
    ingestion.py               ← TextChunker + DocumentLoader
    models.py                  ← Pydantic v2 request/response models
    requirements.txt           ← fastapi, uvicorn[standard], chromadb, pydantic, requests
    chroma_db/                 ← ChromaDB persistent storage (auto-created on first run)
  agents/
    orchestrator/
      system_prompt.md         ← decomposition & routing prompt
      validation_system_prompt.md ← validation decision prompt (complete|refine|additional_work|redo)
    coder/
      inbox/
      system_prompt.md
    research/
      inbox/
      system_prompt.md
    claude-code/
      inbox/                   ← approved tasks ready to run
      pending/                 ← tasks awaiting manual approval before claude CLI executes
    qa/
      inbox/
      system_prompt.md
  dashboard/                   ← real-time web monitoring UI (Flask)
    app.py                     ← REST API server (task, RAG proxy, approval, and chat endpoints)
    run_dashboard.py           ← launcher (reads config.json)
    task_monitor.py            ← filesystem scanner
    templates/index.html       ← dashboard UI (Tasks, Results, Approvals, KB, Chat, Agent Stats tabs)
    static/dashboard.js        ← frontend polling logic + KB management + chat functions
    static/dashboard.css       ← styling
    agent_chat.py              ← chat LLM tool-calling loop (rag_query, web_search, web_fetch; max 8 turns)
    chat_context.py            ← pipeline snapshot builder; deep task context injector
    chat_session.py            ← in-memory UUID-keyed session store; max 20 history turns per session
    chat_system_prompt.md      ← chat system prompt template ({PIPELINE_SNAPSHOT}, {TODAY})
  logs/                        ← per-agent execution traces at logs/<agent>/general.log
  scripts/
    shared/
      task_io.py               ← task file I/O: read/write/move, dependency resolution, validation grouping
      ollama_client.py         ← Ollama wrapper: chat() and chat_with_tools(); sets OLLAMA_API_KEY env var
      token_logger.py          ← appends {ts, task_id, prompt, completion} to logs/<agent>/tokens.jsonl
      web_search.py            ← web_search() and web_fetch() wrappers (ollama Python library)
      rag_tool.py              ← rag_query(query, top_k) → str; POST /query to RAG API; graceful fallback
      file_watcher.py          ← TaskWatcher: watchdog-based file event monitoring for immediate triggering
      logger.py                ← file + stdout logging, UTC timestamps
      config.py                ← config.json loader (ProjectConfig class, includes rag_api_url())
    agent_orchestrator.py      ← 3-phase loop: validate, resolve deps, dispatch; RAG pre-prompt injection
    agent_coder.py             ← RAG pre-prompt injection before user_message construction
    agent_research.py          ← rag_query in TOOLS list (up to 5 RAG calls per task)
    agent_claude_code.py       ← Claude CLI subprocess wrapper with pipeline preamble
    agent_qa.py                ← rag_query in QA_TOOLS list (up to 5 RAG calls per task)
    scheduler.py               ← cross-platform scheduler: file watcher + optional timer + RAG API lifecycle
```

---

## Running the System

**Terminal 1 — Agents + RAG API:**
```bash
RUN_SCHEDULER.bat        # Windows
RUN_SCHEDULER.sh         # Linux / Mac
# or manually:
python scripts/scheduler.py
```

The scheduler starts the RAG API automatically and keeps it alive. Press Ctrl+C to stop everything gracefully.

**Terminal 2 — Dashboard (optional but recommended):**
```bash
python dashboard/run_dashboard.py
# Open http://localhost:5000
```

The dashboard runs independently of the scheduler and can be started or stopped at any time without affecting running agents.

**First-time setup — RAG API dependencies:**
```bash
cd rag_api
pip install -r requirements.txt
```

---

## Task File Format

```markdown
---
id: task_YYYYMMDD_HHMMSS_microseconds   ← microseconds suffix prevents ID collisions
type: research|code|summarize|review|plan|qa
priority: high|medium|low
created_by: user|dashboard|orchestrator|coder|qa
created_at: 2026-05-07T10:00:00
assigned_to: orchestrator|coder|research|claude-code|qa|pending_approval
status: pending|dispatched|processing
output_path: outbox/task_..._result.md
context_files: []             ← populated by dependency resolver when deps complete
parent_task_id: task_...      ← links subtask back to original parent (set by orchestrator)
depends_on: [task_...]        ← blocks processing until listed tasks land in outbox/
chain_to: qa                  ← optional: agent to hand off to after completion
retry_count: 0                ← QA retry counter (max 1 before escalating to failed/)
original_description: ...     ← preserved across retries for QA context
iteration: 1                  ← validation loop iteration counter (max 5, on parent tasks)
stall_retry_count: 0          ← infrastructure retry counter (max 2); incremented on Ollama crash/timeout
validation_context:           ← optional dict: orchestrator reasoning + QA feedback from prior iteration;
                                 injected into redo/refine/additional_work follow-up bodies as
                                 "## Validation Context" so workers understand what previously failed
redecompose_after_research: true  ← flag: orchestrator dispatches only research first, then re-calls
                                     decomposition LLM with research output before planning implementation;
                                     cleared after first re-decomposition to prevent infinite loops
---

## Task Description
...

## Expected Output
...
```

---

## Result File Format

Result files are written to `outbox/` by workers and the orchestrator. Each `.task.md` file has a corresponding `*_result.md` deliverable.

**Subtask result** (written by worker agent):

```markdown
---
task_id: task_YYYYMMDD_HHMMSS_microseconds
agent: research|coder|qa|claude-code
model: qwen3.5:9b|Claude (...)
---

# Result Title

[Worker-generated output: code, research report, QA verdict, etc.]
```

**Parent task result** (written by orchestrator after final validation):

```markdown
---
task_id: task_YYYYMMDD_HHMMSS_microseconds
status: complete
---

# Task Completion Summary

## Decision Reasoning
{Orchestrator's validation rationale}

## Subtask Results

### Research Result (Task: task_...)
[Full research output]

### Code Result (Task: task_...)
[Full implementation]

### QA Result (Task: task_...)
[Full QA verdict and execution results]
```

Subtasks are ordered: research → code → qa → other. Missing result files are replaced with a `[Result file not found: ...]` placeholder.

---

## Agent Reference

| Script | Model | Inbox | Notes |
|---|---|---|---|
| `agent_orchestrator.py` | qwen3.5:9b | `inbox/` | 3-phase loop; RAG pre-prompt injection at decomposition; concurrency lock |
| `agent_coder.py` | qwen3.5:9b | `agents/coder/inbox/` | Skips tasks with unresolved `depends_on`; RAG pre-prompt injection |
| `agent_research.py` | qwen3.5:9b | `agents/research/inbox/` | Tool loop: up to 5 web_search + 10 web_fetch + 5 rag_query turns |
| `agent_claude_code.py` | Claude CLI | `agents/claude-code/inbox/` | Tasks require dashboard approval before running (arrive in `pending/` first) |
| `agent_qa.py` | qwen3.5:9b | `agents/qa/inbox/` | Tool loop: up to 3 web_search + 6 web_fetch + 5 rag_query turns; code execution via subprocess |

Timer intervals (when `enable_timer_polling: true`): orchestrator 0.5 min, research 1 min, coder 1.5 min, qa 2 min, claude-code 2.5 min. Currently **disabled** — agents are triggered exclusively by the file watcher.

---

## File Watcher

`scripts/shared/file_watcher.py` provides immediate agent triggering when `.task.md` files appear.

`TaskWatcher` (built on the `watchdog` library) monitors each task folder for file creation and modification events. When a `.task.md` file is detected, it coalesces rapid bursts within a 0.5-second window, then fires the corresponding agent subprocess via `scheduler.trigger_agent()`.

| Folder watched | Agent triggered |
|---|---|
| `inbox/` | orchestrator |
| `validation/` | orchestrator |
| `agents/coder/inbox/` | coder |
| `agents/research/inbox/` | research |
| `agents/qa/inbox/` | qa |
| `agents/claude-code/inbox/` | claude-code |

On startup, the watcher scans each folder for pre-existing `.task.md` files so tasks already waiting when the scheduler starts are not missed. If `watchdog` is not installed, a warning is logged and the system falls back to timer-only mode (requires `enable_timer_polling: true` in `config.json`).

---

## Orchestrator — 3-Phase Loop

Every cycle (triggered by file watcher or timer) the orchestrator runs three phases in sequence.

**Phase 1 — Validation.** Scans `validation/` for completed subtasks, groups them by `parent_task_id`, and calls the validation LLM for each parent. Returns one of four decisions:

| Decision | Meaning | Action |
|---|---|---|
| `complete` | Work satisfies requirements | Move parent from `processing/` → `outbox/` |
| `refine` | Mostly good; minor improvements needed | Create follow-up subtasks, increment `iteration` |
| `additional_work` | Sound approach but incomplete | Create follow-up subtasks, increment `iteration` |
| `redo` | Does not meet requirements | Create new subtasks with failure context, increment `iteration` |

Maximum 5 iterations per parent task; forced `complete` at the limit to prevent infinite loops.

**Phase 2 — Dependency resolution.** Scans all worker inboxes for tasks with a `depends_on` field. For each, checks whether the dependency's result exists in `outbox/`. When all dependencies are resolved, result paths are wired into `context_files` and `depends_on` is cleared, unblocking the task.

**Phase 3 — Dispatch.** Reads new parent tasks from `inbox/`, calls the decomposition LLM, creates subtasks in worker inboxes, and moves the parent to `processing/` with `status: dispatched`. When research and coder subtasks are created together, the coder task automatically gets `depends_on: [research_task_id]` so it always waits for research output. Ollama timeout: 360s.

If the decomposition LLM determines that research must complete before a good plan can be written, it sets `redecompose_after_research: true` and dispatches only the research subtask(s). When research passes validation, `redecompose_with_research()` re-calls the decomposition LLM with the research output injected, then dispatches the full implementation plan. The flag is cleared immediately after re-decomposition.

---

## Task Flow

```
inbox/          → orchestrator decomposes → agents/*/inbox/   (parent → processing/)
agents/*/inbox/ → worker executes         → validation/       (not outbox/ directly)
validation/     → orchestrator validates  → outbox/ (complete) | back to agents/ (refine/redo)
```

Workers never write their task file directly to `outbox/`. They write their result file to `outbox/` but move their task file to `validation/` via `mark_awaiting_validation()`. Only the orchestrator's `complete` decision moves the parent task to `outbox/`.

---

## QA Loop

All code tasks automatically chain through QA:

1. Orchestrator sets `chain_to: qa` on every code subtask.
2. Coder completes → creates QA task in `agents/qa/inbox/` with the result file in `context_files`. The QA task inherits `parent_task_id` from the coder task.
3. QA extracts code → executes via subprocess (30s timeout) → reviews with qwen3.5:9b. May perform up to 3 `web_search` + 6 `web_fetch` + 5 `rag_query` calls to look up errors or verify library usage.
4. **PASS** → writes approval to `outbox/`, moves task to `validation/`.
5. **FAIL, retry_count=0** → creates new coder task with QA feedback, `retry_count=1`. Retry coder also inherits `parent_task_id`.
6. **FAIL, retry_count=1** → writes failure report to `failed/`, moves task to `validation/`.

The orchestrator's validation gate for code subtasks does not fire until the QA task has completed. If QA is still in-flight, the parent is skipped for that cycle. When QA fails on first attempt, the orchestrator waits for the full retry cycle (retry coder → QA round 2) to resolve before making a validation decision.

---

## Claude Code — Approval Gate

Tasks the orchestrator routes to `claude-code` land in `agents/claude-code/pending/` first, not `inbox/`. This prevents unattended Claude CLI invocations.

**Via dashboard Approvals tab:** lists all pending tasks with Approve and Reject buttons. Approve moves the file to `agents/claude-code/inbox/`; Reject moves it to `failed/` with the rejection reason appended.

**Manually:** move the `.task.md` file from `agents/claude-code/pending/` to `agents/claude-code/inbox/`.

Every prompt sent to the CLI is prefixed with `_PIPELINE_PREAMBLE`, which instructs the CLI to respond via stdout only — not to attempt filesystem writes, use tools, or request permissions in non-interactive mode.

---

## RAG API — Knowledge Base

A standalone FastAPI service in `rag_api/`. Started and monitored by the scheduler — no separate terminal needed.

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness check |
| `/ingest` | POST | Add a document (chunked + embedded into ChromaDB) |
| `/query` | POST | Semantic search; returns top-k chunks with scores |
| `/documents` | GET | List all stored document IDs and metadata |
| `/documents/{id}` | GET | Retrieve a specific document |
| `/documents/{id}` | DELETE | Remove a document |

### Embedding and Reranking

- **Embedding model:** `qwen3-embedding:8b` via `POST /api/embeddings`. Response key: `"embedding"` (flat list, dim=4096).
- **Reranking:** cosine similarity computed from the embedding model — no native `/api/rerank` endpoint exists in Ollama.
- **Fallback vector:** `[0.0] × 4096` used when embedding fails so ingestion never crashes (retrieval quality for that chunk will be zero).

### ChromaDB API Notes

`collection.get()` returns **flat lists** (`ids`, `documents`, `metadatas` are plain Python lists). `collection.query()` returns **nested lists** — one inner list per query vector: `results['ids'][0]`, `results['documents'][0]`, etc. Mixing these up causes IndexError or incorrect results.

### Scheduler Lifecycle

1. After health checks, `_start_rag_api()` launches `uvicorn main:app` as a persistent subprocess in `rag_api/`.
2. Every 30 seconds, `_check_rag_api()` polls the process and restarts it if it has exited.
3. On shutdown, `_stop_rag_api()` calls `process.terminate()` with a 5-second wait, then `process.kill()` as a fallback.

### Agent Integration

**Pre-prompt injection** (coder, orchestrator): before building `user_message`, the agent calls `rag_query(task_body[:500])`. If results are found, they are prepended as `## Knowledge Base Context`. Unavailable or empty results are silently skipped — fully transparent degradation.

**Tool in loop** (research, QA): `rag_query` is added to the `TOOLS` list alongside `web_search` and `web_fetch`. The model decides when to call it, up to `MAX_RAG_TURNS = 5` per task, counted within the same `MAX_TOOL_TURNS` ceiling.

### Dashboard Integration

The dashboard proxies all RAG API calls through Flask to avoid CORS issues:

| Dashboard endpoint | Proxies to |
|---|---|
| `GET /api/rag/status` | `GET /health` |
| `GET /api/rag/documents` | `GET /documents` |
| `POST /api/rag/ingest` | `POST /ingest` |
| `DELETE /api/rag/documents/<id>` | `DELETE /documents/<id>` |

The **Knowledge Base** tab lets you paste text, set a title and source, and ingest documents without touching the CLI. It also lists all stored documents with per-document delete buttons.

---

## Dashboard

A standalone Flask process that reads directly from the filesystem — no database required. Can be started and stopped independently of the scheduler.

```bash
python dashboard/run_dashboard.py           # http://localhost:5000
python dashboard/run_dashboard.py --port 8080 --debug
```

### REST API

| Endpoint | Description |
|---|---|
| `GET /api/status` | System metrics (pending / processing / completed / failed / awaiting_approval counts) |
| `GET /api/tasks` | All tasks; optional `?status=` and `?type=` filters |
| `GET /api/tasks/<id>` | Full task detail: metadata, body, logs, result |
| `GET /api/tasks/<id>/payload` | Raw task file content (frontmatter + body) |
| `GET /api/agents` | Per-agent stats: completed, errors, prompt_tokens, completion_tokens, llm_calls |
| `GET /api/agents/<name>/logs` | Last N log lines for an agent |
| `GET /api/pending-approvals` | Tasks waiting in `agents/claude-code/pending/` |
| `POST /api/pending-approvals/<id>/approve` | Move task to `agents/claude-code/inbox/` |
| `POST /api/pending-approvals/<id>/reject` | Move task to `failed/` with rejection reason |
| `POST /api/tasks/submit` | Create a task in `inbox/` from the dashboard |
| `POST /api/clear-cache` | Delete all task files, logs, and token counters (full system reset) |
| `POST /api/chat` | Chat with the pipeline assistant; can create tasks via `<CREATE_TASK>` blocks |
| `POST /api/chat/clear` | Clear conversation history for a session |
| `GET /api/rag/status` | RAG API liveness |
| `GET /api/rag/documents` | List knowledge base documents |
| `POST /api/rag/ingest` | Add a document to the knowledge base |
| `DELETE /api/rag/documents/<id>` | Remove a document from the knowledge base |

See `DASHBOARD.md` for full API docs and configuration options.

---

## Concurrency and Fault Tolerance

### Concurrency Guard

The orchestrator uses a lockfile (`processing/orchestrator.lock`) with PID validation to prevent concurrent instances. Stale locks from a dead process are cleaned up automatically on the next run.

### Orphan Recovery

Four recovery functions run at orchestrator startup before Phase 1:

1. **`recover_orphaned_tasks()`** — scans `processing/` for `.task.md` files with `status: pending`. These are parent tasks whose LLM decomposition call was interrupted (e.g. killed mid-call). Moved back to `inbox/` to re-enter the pipeline. Tasks with `status: dispatched` or `processing` are not touched.

2. **`recover_processing_subtasks()`** — scans `processing/` for non-orchestrator subtasks with `status: processing` older than 720 seconds. These are tasks claimed by a worker that was killed mid-LLM-call after `mark_processing()` but before finishing. Detection is time-based: 720s exceeds any realistic LLM call including max tool turns. Recovery: reset status to `pending` and return to the worker inbox. Does not increment `stall_retry_count` — this is infrastructure failure recovery, not task-content failure.

3. **`recover_stalled_subtasks()`** — scans `failed/` for subtask files whose parent is still in `processing/`. Groups by parent. If `stall_retry_count < 2`: resets and retries, incrementing `stall_retry_count` on the parent. If max retries exhausted: writes failure report and moves parent to `failed/`. Subtasks whose parent is in `outbox/` are silently skipped.

4. **`recover_orphaned_validation_subtasks()`** — sweeps `validation/` for stranded subtasks. Two detection cases: (a) subtask has `parent_task_id` pointing to a `status: complete` parent in `outbox/`; (b) subtask has no `parent_task_id` but its `output_path` result file already exists in `outbox/`. Both cases move the subtask to `outbox/` via `mark_completed()`.

### SIGINT Isolation

Agent subprocesses are spawned with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` (Windows) or `start_new_session=True` (Unix). This isolates agent processes from the scheduler's console signal group so Ctrl+C in the scheduler terminal stops the scheduler gracefully without propagating to any in-flight agent LLM call.

---

## Scheduler Startup Sequence

Before spawning any agent subprocesses, `scheduler.py` performs these steps in order:

1. **Ollama availability check** — pings the Ollama server; logs a warning and continues if unreachable. Agents will fail on their first LLM call rather than at startup.
2. **Flush `.pyc` caches** — recursively deletes all `__pycache__` directories under `scripts/` so agents always import fresh bytecode. Prevents stale cached modules from masking recent edits.
3. **Health-check import** — test-imports `scripts/shared/task_io.py`. If import fails (syntax error, truncation, etc.), logs a `FATAL` error and aborts without starting any agents.
4. **Start RAG API** — launches `uvicorn main:app` as a persistent subprocess in `rag_api/`. If `uvicorn` is not installed, logs a warning and continues; agents degrade gracefully (unavailability returns a plain string, not an exception).
5. **Initialize file watchers** — starts `TaskWatcher` for all task folders. Falls back to timer-only mode if `watchdog` is not installed.
6. **Start scheduling loop** — if `enable_timer_polling: true`, initialises timer-based scheduling. The main loop also calls `_check_rag_api()` every 30 seconds to restart the RAG API if it has exited.

---

## Configuration

All runtime settings live in `config.json` at the project root, loaded by `scripts/shared/config.py`:

```json
{
  "web_search": { "ollama_api_key": "<your Ollama API key>" },
  "ollama": { "base_url": "http://192.168.1.13:11434", "timeout": 360 },
  "agents": {
    "orchestrator": { "model": "qwen3.5:9b",  "process_timeout": 600  },
    "coder":        { "model": "qwen3.5:9b",  "process_timeout": 600  },
    "research":     { "model": "qwen3.5:9b",  "process_timeout": 1800 },
    "qa":           { "model": "qwen3.5:9b",  "process_timeout": 1200 },
    "claude-code":  { "cli": true, "timeout": 300, "process_timeout": 600 }
  },
  "scheduler": { "enable_timer_polling": false },
  "dashboard": { "port": 5000, "debug": false, "poll_interval": 1500 },
  "chat": { "model": "qwen3.5:9b", "timeout": 120, "max_history_turns": 20, "max_tool_turns": 8 },
  "rag_api": { "url": "http://localhost:8000" }
}
```

**Key fields:**

- `ollama.timeout` — Ollama request timeout per individual LLM call (360s).
- `agents.<name>.process_timeout` — scheduler-level kill ceiling for the full agent subprocess. Must exceed `ollama.timeout × max_tool_turns`. Research is set to 1800s to accommodate long multi-fetch loops.
- `scheduler.enable_timer_polling` — `false` (default) means agents are triggered only by the file watcher. Set to `true` to add timer-based polling as a fallback or supplement.
- `rag_api.url` — URL of the RAG API service; accessible via `ProjectConfig.rag_api_url()`.

---

## Ollama Integration

Both plain chat and tool-calling modes use `scripts/shared/ollama_client.py` (backed by the `ollama` Python library).

**Critical:** `OLLAMA_API_KEY` must be set in the environment before the `ollama` library is imported — the library reads the env var at module initialisation. `ollama_client.py` is always the first shared import in every agent script and sets `os.environ["OLLAMA_API_KEY"]` from `config.json` before `import ollama` runs.

**Plain chat** (`OllamaClient.chat()`) — used by orchestrator and coder:
```python
ollama.Client(host=...).chat(model, messages, options)
```

**Tool-calling loop** (`OllamaClient.chat_with_tools()`) — used by research and QA. Tools are passed as **Python callables**: the `ollama` library introspects type annotations and docstrings to auto-generate JSON schemas — no manual tool-definition dicts needed.

```python
result = client.chat_with_tools(model, messages, tools=[web_search, web_fetch, rag_query])
```

Returns `{"type": "text", ...}` or `{"type": "tool_call", "name": ..., "arguments": {...}, "raw_message": <Message>}`. The agent executes the tool, appends results to message history, and loops until a text response is received or the turn limit is hit.

**Web tools** (`scripts/shared/web_search.py`):

| Function | Backed by | Purpose |
|---|---|---|
| `web_search(query, max_results)` | `ollama.web_search()` | Title + URL + snippet per result |
| `web_fetch(url)` | `ollama.web_fetch()` | Full page title + content |

---

## Token Logging

After every successful Ollama call, each agent appends token usage to `logs/<agent>/tokens.jsonl`:

```json
{"ts": "2026-05-07T10:00:01Z", "task_id": "task_20260507_...", "prompt": 312, "completion": 87}
```

The log is append-only and accumulates across all runs. The dashboard **Agent Stats** tab reads these files and displays cumulative `Prompt Tokens`, `Completion Tokens`, and `LLM Calls` per agent. The `claude-code` worker always shows `—` (no Ollama calls).

---

## Diagrams

- `ai-team-architecture.drawio` — System topology
- `ai-team-message-flows.drawio` — Message flow and QA loop
