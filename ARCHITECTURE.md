# AI Team — Multi-Agent Architecture

> Last updated: 2026-05-16

## Overview

A team of agents coordinated through this shared folder. Agents are triggered by a file watcher (immediate) and optionally by a timer-based polling fallback, both managed by `scheduler.py`. Ollama runs at `http://192.168.1.13:11434`.

## Topology

```
[João / Claude (Cowork)]
        │  writes tasks
        ▼
   inbox/
        │
        ▼  file watcher triggers immediately; timer fallback every 0.5 min
[Orchestrator: qwen3.5:9b]                    ←── rag_query() pre-prompt injection
   Phase 1 — VALIDATION: scan validation/, group by parent, LLM decides complete|refine|additional_work|redo
   Phase 2 — DEPENDENCY RESOLUTION: unblock tasks whose depends_on are satisfied, wire context_files
   Phase 3 — DISPATCH: decompose new inbox tasks, route to worker inboxes
        │
   ┌────┼────────────────────┐
   ▼    ▼                    ▼
[Coder]  [Research]   [Claude Code*]
qwen3.5  qwen3.5:9b      claude CLI
  :9b    + web_search    *requires approval
  ↑RAG   + web_fetch                            ← rag_query tool in tool loop
context  + rag_query
injection
   │ chain_to: qa
   ▼
[QA Agent: qwen3.5:9b + web_search + web_fetch + rag_query]
   - Extracts code, executes via subprocess (30s timeout)
   - Reviews with qwen3.5:9b; may search for error/library docs or query knowledge base
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

                    ┌──────────────────────────────────┐
                    │  RAG API (FastAPI + ChromaDB)     │
                    │  http://localhost:8000            │
                    │  Embed: qwen3-embedding:8b (4096) │
                    │  Rerank: cosine similarity        │
                    │  Started + monitored by scheduler │
                    └──────────────────────────────────┘
                           ↑ rag_query() calls from all agents
                           ↑ ingest via dashboard KB tab or POST /ingest
```

## Folder Structure

```
AI Team/
  ARCHITECTURE.md              ← this file
  CLAUDE.md                    ← project instructions for Claude (Cowork)
  DASHBOARD.md                 ← dashboard usage and REST API reference
  config.json                  ← runtime config (Ollama URL, models, dashboard port, rag_api.url)
  RUN_SCHEDULER.bat            ← Windows quick-start (agents + RAG API)
  RUN_SCHEDULER.sh             ← Linux/Mac quick-start
  inbox/                       ← drop task files here to start work
  processing/                  ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/                  ← completed subtasks awaiting orchestrator approval
  outbox/                      ← approved & truly completed results
  failed/                      ← QA failure reports + hard-errored tasks
  context/                     ← optional shared context files for tasks
  rag_api/                     ← Local knowledge base service
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
    app.py                     ← REST API server (includes chat, RAG proxy, and approval endpoints)
    run_dashboard.py           ← launcher (reads config.json)
    task_monitor.py            ← filesystem scanner
    templates/index.html       ← dashboard UI (includes Knowledge Base and Chat tabs)
    static/dashboard.js        ← frontend polling logic + KB management + chat functions
    static/dashboard.css       ← styling
    agent_chat.py              ← chat LLM tool-calling loop (rag_query, web_search, web_fetch; max 8 turns)
    chat_context.py            ← pipeline snapshot builder; deep task context injector (body, result, logs)
    chat_session.py            ← in-memory UUID-keyed session store; max 20 history turns per session
    chat_system_prompt.md      ← chat system prompt template; {PIPELINE_SNAPSHOT} and {TODAY} placeholders
  logs/                        ← per-agent execution traces at logs/<agent>/general.log
  scripts/
    shared/
      task_io.py               ← task file I/O: read/write/move, dependency resolution, validation grouping
      ollama_client.py         ← Ollama wrapper: chat() and chat_with_tools(); sets OLLAMA_API_KEY env var before importing ollama library
      token_logger.py          ← appends {ts, task_id, prompt, completion} to logs/<agent>/tokens.jsonl
      web_search.py            ← web_search() and web_fetch() wrappers delegating to ollama Python library
      rag_tool.py              ← rag_query(query, top_k) → str; POST /query to RAG API; graceful fallback
      file_watcher.py          ← TaskWatcher: watchdog-based file event monitoring for immediate agent triggering
      logger.py                ← file + stdout logging, UTC timestamps
      config.py                ← config.json loader (ProjectConfig class, includes rag_api_url())
    agent_orchestrator.py      ← 3-phase loop: validate, resolve deps, dispatch; RAG pre-prompt injection
    agent_coder.py             ← RAG pre-prompt injection before user_message construction
    agent_research.py          ← rag_query in TOOLS list (up to 5 RAG calls per task)
    agent_claude_code.py
    agent_qa.py                ← rag_query in QA_TOOLS list (up to 5 RAG calls per task)
    scheduler.py               ← cross-platform scheduler: file watcher + optional timer polling + RAG API process management
```

## Task File Format

```markdown
---
id: task_YYYYMMDD_HHMMSS_microseconds   ← microseconds suffix prevents ID collisions
type: research|code|summarize|review|plan|qa
priority: high|medium|low
created_by: claude-cowork|orchestrator|coder|qa|dashboard
created_at: 2026-05-07T10:00:00
assigned_to: orchestrator|coder|research|claude-code|qa|pending_approval
status: pending|dispatched|processing   ← pending=awaiting orchestrator; dispatched=subtasks created, waiting validation; processing=worker running it
output_path: outbox/task_..._result.md
context_files: []             ← populated by dependency resolver when deps complete
parent_task_id: task_...      ← links subtask back to original parent (set by orchestrator)
depends_on: [task_...]        ← blocks processing until listed tasks land in outbox/
chain_to: qa                  ← optional: agent to hand off to after completion
retry_count: 0                ← QA retry counter (max 1 before escalating to failed/)
original_description: ...     ← preserved across retries for QA context
iteration: 1                  ← validation loop iteration counter (max 5, on parent tasks)
stall_retry_count: 0          ← infrastructure retry counter (max 2); incremented by recover_stalled_subtasks()
                                 when a subtask fails due to Ollama timeout/crash, not task-content failure
validation_context:           ← optional dict: orchestrator reasoning + QA feedback from prior iteration,
                                 injected into redo/refine/additional_work follow-up subtask bodies as
                                 a "## Validation Context" section so workers know what previously failed
redecompose_after_research: true  ← optional flag on parent tasks: orchestrator dispatches only research
                                     first, then re-calls decomposition LLM with research context to produce
                                     implementation subtasks; flag is cleared after first re-decomposition
                                     to prevent infinite loops
---

## Task Description
...

## Expected Output
...
```

## Result File Format

Result files are written to `outbox/` by workers and the orchestrator. Each `.task.md` file has a corresponding `*_result.md` file containing the deliverable output.

**Subtask Result** (`outbox/task_..._result.md` — written by worker agent):

```markdown
---
task_id: task_YYYYMMDD_HHMMSS_microseconds
agent: research|coder|qa|claude-code
model: qwen3.5:9b|Claude (...)
---

# Result Title

[Worker-generated output: code, research report, QA verdict, etc.]
```

**Parent Task Result** (`outbox/task_parent_result.md` — written by orchestrator after validation):

When a parent task completes, the orchestrator aggregates all approved subtask results into a single parent result file. The format includes the orchestrator's validation summary, followed by the actual deliverables:

```markdown
---
task_id: task_YYYYMMDD_HHMMSS_microseconds
status: complete
---

# Task Completion Summary

Task {parent_task_id} completed after validation.

## Decision Reasoning

{Orchestrator's validation rationale — why this task was approved}

## Subtask Results

### Research Result (Task: task_...)

[Full research output from research subtask]

### Code Result (Task: task_...)

[Full code/implementation from coder subtask]

### Qa Result (Task: task_...)

[Full QA verdict, execution results, and review from QA subtask]
```

**Result Aggregation Rules:**

- Parent results always include decision reasoning (why it was approved)
- All approved subtask outputs are aggregated in a single parent result file
- Subtasks are ordered by type: research → code → qa → other types
- Each subtask result is included in full (not summarized or truncated)
- Missing result files are handled gracefully with a `[Result file not found: ...]` placeholder

## Agent Scripts

All scripts live in `scripts/`. Each is standalone and invoked by the scheduler.

| Script | Model | Inbox | Timer interval* | Notes |
|---|---|---|---|---|
| `agent_orchestrator.py` | qwen3.5:9b | `inbox/` | 0.5 min | 3-phase loop; RAG pre-prompt injection at decomposition |
| `agent_coder.py` | qwen3.5:9b | `agents/coder/inbox/` | 1.5 min | skips tasks with unresolved `depends_on`; RAG pre-prompt injection |
| `agent_research.py` | qwen3.5:9b | `agents/research/inbox/` | 1 min | web: 5 search + 10 fetch + 5 RAG calls max (20 total turns) |
| `agent_claude_code.py` | Claude Code CLI | `agents/claude-code/inbox/` | 2.5 min | tasks arrive via manual approval from `pending/` |
| `agent_qa.py` | qwen3.5:9b | `agents/qa/inbox/` | 2 min | web: 3 search + 6 fetch + 5 RAG calls max (14 total turns) |

\* Timer intervals only apply when `scheduler.enable_timer_polling: true` in `config.json`. Currently **disabled** — agents are triggered exclusively by the file watcher (see below).

## File Watcher

`scripts/shared/file_watcher.py` provides immediate agent triggering when `.task.md` files appear, replacing the need for constant timer polling.

**How it works:**

`TaskWatcher` (using the `watchdog` library) monitors each task folder for file creation and modification events. When a `.task.md` file is detected, it coalesces rapid bursts within a 0.5-second window, then fires the corresponding agent as a subprocess via `scheduler.trigger_agent()`.

**Folders watched:**

| Folder | Agent triggered |
|---|---|
| `inbox/` | orchestrator |
| `validation/` | orchestrator |
| `agents/coder/inbox/` | coder |
| `agents/research/inbox/` | research |
| `agents/qa/inbox/` | qa |
| `agents/claude-code/inbox/` | claude-code |

**Startup scan:** on start, the watcher scans each folder for pre-existing `.task.md` files and immediately triggers the corresponding agent, so tasks already waiting when the scheduler starts are not missed.

**Graceful degradation:** if `watchdog` is not installed, a warning is logged and the system falls back to timer-only mode (requires `enable_timer_polling: true`).

**Timer polling toggle:** `config.json → scheduler.enable_timer_polling` (default `true` if missing). Set to `false` to rely exclusively on the file watcher (current production setting).

## Orchestrator — 3-Phase Loop

Every cycle (triggered by file watcher or timer) the orchestrator runs three phases in sequence:

**Phase 1 — Validation.** Scans `validation/` for completed subtasks, groups them by `parent_task_id`, then calls the validation LLM (`validation_system_prompt.md`) for each parent. The LLM returns one of four decisions:

| Decision | Meaning | Action |
|---|---|---|
| `complete` | Work satisfies requirements | Move parent from `processing/` → `outbox/` |
| `refine` | Mostly good, minor improvements needed | Create follow-up subtasks, increment `iteration` |
| `additional_work` | Sound approach but incomplete | Create follow-up subtasks, increment `iteration` |
| `redo` | Does not meet requirements | Create new subtasks with failure context, increment `iteration` |

Maximum 5 iterations per parent task; forced `complete` at the limit to prevent infinite loops.

If a subtask in `validation/` references a parent task that no longer exists in `processing/`, the orchestrator checks `outbox/` before acting: if the parent is there with `status: complete`, the subtask is moved to `outbox/` rather than `failed/`. Only if the parent is missing from both locations is the subtask moved to `failed/`.

**Phase 2 — Dependency resolution.** Scans all worker inboxes for tasks with a `depends_on` field. For each, checks whether the dependency's result file exists in `outbox/`. If all dependencies are resolved, it wires the result paths into `context_files` and removes `depends_on`, unblocking the task.

**Phase 3 — Dispatch.** Reads new parent tasks from `inbox/`, calls the decomposition LLM (`system_prompt.md`), creates subtasks in worker inboxes, and moves the parent to `processing/` with `status: dispatched`. The `dispatched` status prevents orphan recovery from re-queueing the parent on subsequent cycles. When research and coder subtasks are created together, the coder task is automatically given `depends_on: [research_task_id]` so it waits for the research output before running. Ollama timeout is set to 360s (`config.json`) to accommodate complex decomposition calls.

If the decomposition LLM determines that research must complete before a good implementation plan can be written, it can set `redecompose_after_research: true` on the parent and dispatch only the research subtask(s). When the research result reaches Phase 1 validation and is approved, `redecompose_with_research()` re-calls the decomposition LLM with the research output injected as context, then dispatches the implementation subtasks. The `redecompose_after_research` flag is cleared immediately after the first re-decomposition to prevent the cycle from triggering again.

## Task Flow

```
inbox/          → orchestrator decomposes → agents/*/inbox/   (parent moves to processing/)
agents/*/inbox/ → worker executes         → validation/       (not outbox/)
validation/     → orchestrator validates  → outbox/ (complete) or back to agents/ (refine/redo)
```

Workers never write directly to `outbox/`. They write their result file to `outbox/` but move their task file to `validation/` via `mark_awaiting_validation()`. Only the orchestrator's `complete` decision moves the parent task to `outbox/`.

## Claude Code — Approval Gate

Tasks the orchestrator routes to `claude-code` go to `agents/claude-code/pending/` (not `inbox/`) until approved. This prevents unattended claude CLI invocations.

**Via dashboard:** the **Approvals** tab lists all pending tasks with Approve and Reject buttons. Approve moves the file to `agents/claude-code/inbox/`; Reject moves it to `failed/` with the rejection reason appended.

**Manually:** move the `.task.md` file from `agents/claude-code/pending/` to `agents/claude-code/inbox/`.

Once in `agents/claude-code/inbox/`, the agent picks it up on its next trigger cycle.

## QA Loop

All code tasks automatically chain through QA:

1. Orchestrator sets `chain_to: qa` on every code subtask.
2. Coder completes → creates QA task in `agents/qa/inbox/` with the result file in `context_files`. The QA task inherits `parent_task_id` from the coder task so it is linked to the original parent.
3. QA: extracts code → executes via subprocess (30s timeout) → reviews with qwen3.5:9b.  
   May perform up to 3 `web_search` + 6 `web_fetch` calls (9 tool turns total) to look up errors or verify library usage.
4. **PASS** → writes approval to `outbox/`, moves task to `validation/`.
5. **FAIL, retry_count=0** → creates new coder task with QA feedback, `retry_count=1`. The retry coder task also inherits `parent_task_id` so it remains linked to the original parent.
6. **FAIL, retry_count=1** → writes failure report to `failed/`, moves task to `validation/`.

## Token Logging

After every successful Ollama call, each agent logs token usage to `logs/<agent>/tokens.jsonl`:

```json
{"ts": "2026-05-07T10:00:01Z", "task_id": "task_20260507_...", "prompt": 312, "completion": 87}
```

Helper: `scripts/shared/token_logger.py` — `log_tokens(agent_name, task_id, prompt_tokens, completion_tokens)`. The log is append-only; it accumulates across all runs. The dashboard **Agent Stats** tab reads these files and displays cumulative `Prompt Tokens`, `Completion Tokens`, and `LLM Calls` per agent. `claude-code` always shows `—` (no Ollama calls).

## Ollama API

Both modes use `scripts/shared/ollama_client.py`:

**Critical: OLLAMA_API_KEY must be set before the ollama library is imported.** The library reads the env var during module initialisation. `ollama_client.py` is always the first shared import in every agent script, so it sets `os.environ["OLLAMA_API_KEY"]` from `config.json` before `import ollama` runs. Any later attempt to set the key is silently ignored.

**Plain chat** (`OllamaClient.chat()`) — used by orchestrator, coder:
```
ollama.Client(host=...).chat(model, messages, options)
```

**Tool-calling loop** (`OllamaClient.chat_with_tools()`) — used by research and QA:

Tools are passed as **Python callables** — the ollama library introspects their type annotations and docstrings to auto-generate JSON schemas, so no manual tool-definition dicts are needed.

```python
# tools are the actual functions, not JSON dicts
result = client.chat_with_tools(model, messages, tools=[web_search, web_fetch])
```

Returns `{"type": "text", ...}` or `{"type": "tool_call", "name": "web_search"|"web_fetch", "arguments": {...}, "raw_message": <Message>}`. The agent executes the tool, appends results to message history, and loops until a text response is received or the turn limit is hit.

**Web tools** (`scripts/shared/web_search.py`) — backed by Ollama's cloud API via the ollama Python library:

| Function | Backed by | Purpose |
|---|---|---|
| `web_search(query, max_results)` | `ollama.web_search()` | Returns title + URL + snippet for each result |
| `web_fetch(url)` | `ollama.web_fetch()` | Returns full page title + content |

API key stored in `config.json` under `web_search.ollama_api_key` and exposed via `config.web_search_api_key()`. Set into `OLLAMA_API_KEY` env var by `ollama_client.py` at import time.

## Claude Code Worker

Every prompt is prefixed with `_PIPELINE_PREAMBLE` before being passed to the CLI:

```python
prompt = _PIPELINE_PREAMBLE + task_body
subprocess.run(["claude", "--print", "-p", prompt], capture_output=True, text=True)
```

The preamble instructs the agent to write its complete response as plain text in stdout — not to attempt filesystem writes, use tools, or request permissions. Without this, the CLI running non-interactively may emit a permission-request string instead of the actual output when it infers the task expects a file write.

## Concurrency

The orchestrator uses a lockfile (`processing/orchestrator.lock`) to prevent concurrent instances. Stale locks (dead PID) are cleaned up automatically on the next run.

**Orphan recovery.** Four recovery functions run at startup before Phase 1:

1. `recover_orphaned_tasks()` — scans `processing/` for `.task.md` files with `status: pending`. These are parent tasks whose LLM decomposition call never finished (e.g. killed mid-call). They are moved back to `inbox/` so they re-enter the pipeline. Tasks with `status: dispatched` or `status: processing` are not touched.

2. `recover_processing_subtasks()` — scans `processing/` for subtask files (non-orchestrator) with `status: processing` that are older than 720 seconds (12 minutes). These are tasks claimed by a worker that was killed mid-LLM-call (Ollama timeout, OOM, Ctrl+C) after `mark_processing()` but before finishing. Detection is time-based: 720s comfortably exceeds any realistic LLM call ceiling even with max tool turns. Recovery: reset `status` to `pending` and return the task to the appropriate worker inbox. This is an infrastructure failure recovery path — the parent task's `stall_retry_count` is not incremented here.

3. `recover_stalled_subtasks()` — scans `failed/` for subtask files (not QA failure reports) whose parent task is still in `processing/`. Groups by parent, then: if the parent has not exceeded `stall_retry_count` (max 2), resets and retries the subtasks by returning them to the worker inbox and incrementing `stall_retry_count` on the parent; if max retries are exhausted, writes a failure report and moves the parent to `failed/`. Subtasks whose parent is already in `outbox/` are silently skipped (stale, not stalled).

4. `recover_orphaned_validation_subtasks()` — sweeps `validation/` for subtasks that were stranded because the in-cycle belt-and-suspenders pass didn't reach them (most commonly QA tasks and retry coders that lacked `parent_task_id` before that propagation was added). Two detection cases: (a) subtask has `parent_task_id` and that parent is in `outbox/` with `status: complete`; (b) subtask has no `parent_task_id` but its `output_path` result file already exists in `outbox/`. Both cases move the subtask to `outbox/` via `mark_completed()`.

**SIGINT isolation.** Each agent subprocess is spawned with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows or `start_new_session=True` on Unix. This isolates agent processes from the scheduler's console signal group, so pressing Ctrl+C in the scheduler terminal stops the scheduler gracefully without propagating the signal to any in-flight agent LLM call.

## Configuration

All runtime settings in `config.json` at the project root, loaded via `scripts/shared/config.py`:

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
  "chat": { "model": "qwen3.5:9b", "timeout": 120, "max_history_turns": 20, "max_tool_turns": 8 }
}
```

**Key config fields:**

- `ollama.timeout` — Ollama request timeout in seconds (360s). Applies to each individual LLM call.
- `agents.<name>.process_timeout` — scheduler-level kill timeout for the entire agent subprocess. Must be larger than `ollama.timeout × max_tool_turns` to avoid killing an agent mid-tool-loop. Research is set to 1800s (30 min) to accommodate long multi-fetch loops.
- `scheduler.enable_timer_polling` — when `false` (default), agents are triggered only by the file watcher. Set to `true` to enable interval-based polling as a fallback or supplement.

## RAG API

The knowledge base is a standalone FastAPI service living in `rag_api/`. It is started and monitored by the scheduler — no separate terminal needed.

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

- **Embedding model:** `qwen3-embedding:8b` via `POST /api/embeddings` on the Ollama server. Response key is `"embedding"` (flat list, dim=4096).
- **Reranking:** cosine similarity computed from the same embedding model — no native `/api/rerank` endpoint exists in Ollama. Documents are re-scored by computing `cosine_sim(query_embedding, doc_embedding)` and sorted descending.
- **Fallback vector:** `[0.0] * 4096` used when embedding fails (so ingestion/query never crashes, though retrieval quality for that chunk will be zero).

### ChromaDB API Quirks

`collection.get()` (for listing or fetching by ID) returns **flat lists**: `ids`, `documents`, `metadatas` are plain Python lists. `collection.query()` (for semantic search) returns **nested lists** — one inner list per query vector: `results['ids'][0]`, `results['documents'][0]`, etc. Mixing these up causes IndexError or returns wrong data.

### Scheduler Lifecycle

The `AgentScheduler` manages the RAG API as a persistent Popen subprocess:

1. After health checks and `.pyc` flush, `_start_rag_api()` launches `uvicorn main:app --host 0.0.0.0 --port 8000` in the `rag_api/` directory.
2. Every 30 seconds in the main loop, `_check_rag_api()` calls `process.poll()`. If the process has exited, it restarts automatically.
3. On `KeyboardInterrupt`, `_stop_rag_api()` calls `process.terminate()` with a 5-second wait, then `process.kill()` as a fallback.

### Agent Integration

Two integration patterns are used, depending on agent type:

**Pre-prompt injection** (coder, orchestrator) — before building `user_message`, the agent calls `rag_query(task_body[:500])`. If results are found, they are prepended as a `## Knowledge Base Context` section. If the RAG API is unavailable or returns no results, the message is left unchanged — fully transparent degradation.

**Tool in loop** (research, QA) — `rag_query` is added to the `TOOLS` / `QA_TOOLS` list alongside `web_search` and `web_fetch`. The model decides when to call it during its reasoning loop, up to `MAX_RAG_TURNS = 5` calls per task, counted against the same `MAX_TOOL_TURNS` ceiling. The tool follows the same callable-with-type-annotations pattern as the web tools so the Ollama library auto-generates its JSON schema.

### Dashboard Integration

The dashboard proxies all RAG API calls through Flask endpoints to avoid CORS issues:

| Dashboard endpoint | Proxies to RAG API |
|---|---|
| `GET /api/rag/status` | `GET /health` |
| `GET /api/rag/documents` | `GET /documents` |
| `POST /api/rag/ingest` | `POST /ingest` |
| `DELETE /api/rag/documents/<id>` | `DELETE /documents/<id>` |

The **Knowledge Base** tab in the dashboard lets João paste text/docs, set a title and source, and ingest them without touching the CLI. It also lists all stored documents and provides per-document delete buttons.

## Scheduler Startup Sequence

Before spawning any agent subprocesses, `scheduler.py run()` performs these steps in order:

1. **Ollama availability check** — pings the Ollama server; logs a warning and continues if unreachable (agents will fail on their first LLM call rather than at startup).

2. **Flush `.pyc` caches** — recursively deletes all `__pycache__` directories under `scripts/` so agents always import fresh source bytecode. Prevents stale cached modules from masking syntax errors in recently edited files.

3. **Health-check import** — test-imports `scripts/shared/task_io.py` using `importlib.util`. If the import fails (syntax error, truncation, etc.) the scheduler logs a `FATAL` error and returns without starting any agents.

4. **Start RAG API** — launches `uvicorn main:app` as a persistent `Popen` subprocess in the `rag_api/` directory. Logs success or failure to the scheduler log. If `uvicorn` is not installed, a warning is logged and the scheduler continues (agents will degrade gracefully — RAG unavailability returns a plain string, not an exception).

5. **Initialize file watchers** — starts `TaskWatcher` for all task folders. Falls back gracefully to timer-only mode if `watchdog` is not installed.

6. **Start scheduling loop** — if `enable_timer_polling: true`, also initialises timer-based scheduling. The main loop also calls `_check_rag_api()` every 30 seconds to restart the RAG API if it has exited.

## QA Feedback

When QA issues a `FAIL` verdict it creates a retry coder task whose body includes the full `FEEDBACK:` block from the LLM review. The regex `r"FEEDBACK:\s*(.*)"` with `re.DOTALL` captures everything from the `FEEDBACK:` marker to the end of the response, preserving multi-line feedback. The coder receives the complete list of issues rather than a truncated first line.

## Dashboard

A separate Flask process, reads directly from the filesystem (no DB). Start independently:

```bash
python dashboard/run_dashboard.py           # http://localhost:5000
python dashboard/run_dashboard.py --port 8000 --debug
```

REST endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/status` | System metrics (pending/processing/completed/failed/awaiting_approval counts) |
| `GET /api/tasks` | All tasks; optional `?status=` and `?type=` filters |
| `GET /api/tasks/<id>` | Full task detail: metadata, body, logs, result |
| `GET /api/tasks/<id>/payload` | Raw task file content (frontmatter + body) |
| `GET /api/agents` | Per-agent stats: completed, errors, prompt_tokens, completion_tokens, llm_calls |
| `GET /api/agents/<name>/logs` | Last N log lines for an agent |
| `GET /api/pending-approvals` | Tasks waiting in `agents/claude-code/pending/` |
| `POST /api/pending-approvals/<id>/approve` | Move task to `agents/claude-code/inbox/` |
| `POST /api/pending-approvals/<id>/reject` | Move task to `failed/` with rejection reason |
| `POST /api/tasks/submit` | Create a task in `inbox/` directly from the dashboard |
| `POST /api/clear-cache` | Delete