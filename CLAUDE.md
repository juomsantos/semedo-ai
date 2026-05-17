# AI Team — Agent Coordination System

This project is a multi-agent AI coordination system. Agents communicate through a shared filesystem. A file watcher (`scripts/shared/file_watcher.py`) triggers agents immediately when tasks arrive; timer-based polling is an optional fallback. A real-time web dashboard is available at `http://localhost:5000`. See `ARCHITECTURE.md` for the full design and `DASHBOARD.md` for dashboard usage.

## Current Status: Fully Implemented ✓

All agents are built and running with a continuous **orchestrator validation loop**: completed subtask results flow into `validation/`, the orchestrator reviews them, and decides whether to accept, refine, or request more work — up to 5 iterations before forcing completion.

## What's Running

A three-tier multi-agent system with a shared knowledge base:

1. **Claude (Cowork)** — master coordinator, writes tasks to `inbox/`
2. **Orchestrator** (`qwen3.5:9b`) — triggered by file watcher (immediate) or timer fallback (0.5 min); runs 3 phases per cycle: validate completed work → resolve task dependencies → decompose and dispatch new tasks; queries RAG API before decomposition to surface relevant prior work
3. **Workers:**
   - `qwen3.5:9b` (coder) — code generation; skips tasks with unresolved dependencies; queries RAG API for relevant documentation before processing each task
   - `qwen3.5:9b` (research) — research, summarization, Q&A; live `web_search` (up to 5 calls) + `web_fetch` (up to 10 calls) + `rag_query` (up to 5 calls) via Ollama Python library tool loop
   - `claude CLI` (claude-code) — complex/reasoning tasks; tasks require manual approval first (land in `agents/claude-code/pending/` before `inbox/`)
   - `qwen3.5:9b` (qa) — code review + execution testing; live `web_search` (up to 3 calls) + `web_fetch` (up to 6 calls) + `rag_query` (up to 5 calls) via Ollama Python library tool loop

4. **RAG API** (`FastAPI` + `ChromaDB`) — local knowledge base at `http://localhost:8000`; started automatically by the scheduler; accepts documents via POST `/ingest`, serves semantic search via POST `/query`. Embedding: `qwen3-embedding:8b` (dim=4096). Reranking: cosine similarity via the embedding model (no native Ollama `/api/rerank` endpoint required).
5. **Dashboard** (`Flask`) — real-time web UI at `http://localhost:5000`; start with `python dashboard/run_dashboard.py`; includes **Knowledge Base** tab for ingesting and managing documents

## Key Technical Decisions

- **RAG API** (`rag_api/main.py`) — FastAPI + ChromaDB persistent vector store. Embedding via `qwen3-embedding:8b` at `http://192.168.1.13:11434/api/embeddings` (response key `"embedding"`, dim=4096). Reranking implemented as cosine similarity using the embedding model (no native `/api/rerank` endpoint in Ollama). Config: `rag_api/config.py` uses plain `class Settings` with `os.getenv()` — **not** `pydantic_settings.BaseSettings` (not installed). ChromaDB `collection.get()` returns **flat lists**; `collection.query()` returns **nested lists** (one row per query vector) — critical distinction for indexing. URL configured in `config.json → rag_api.url` (default `http://localhost:8000`), accessible via `ProjectConfig.rag_api_url()`. Dependencies in `rag_api/requirements.txt`.
- **RAG tool** (`scripts/shared/rag_tool.py`) — `rag_query(query: str, top_k: int = 5) -> str` follows the same pattern as `web_search.py`; returns a plain `str` (not a dict) so it is compatible with the Ollama tool-calling library which introspects type annotations. Graceful degradation: returns a plain error string (not an exception) if the RAG API is unavailable, so the tool loop continues normally without crashing.
- **RAG context injection** — two modes of integration, chosen by what kind of LLM call the agent makes: (1) **tool mode** — research and QA agents use `chat_with_tools` and include `rag_query` in their `TOOLS` list; the model decides when to call it (up to 5 `rag_query` turns per task, combined with web search/fetch turns in the same `MAX_TOOL_TURNS` ceiling); (2) **pre-prompt injection** — coder and orchestrator use `client.chat` (no tool loop, so no way for the model to call `rag_query` mid-turn). They call `shared/rag_injection.py::inject_rag_context(task_body)` *before* building `user_message`; it queries the RAG API with the first 500 chars and prepends results as a `## Knowledge Base Context` section. Unavailable/error/no-results responses from `rag_query` are filtered out via `_NON_USEFUL_PREFIXES` so the task body is returned unchanged when the API is down — behaviour is identical to no injection at all. The helper is the single source of truth; both call sites previously had copy-pasted byte-for-byte blocks (audit finding M2).
- **RAG API lifecycle** — `scheduler.py` manages the RAG API as a persistent subprocess (`subprocess.Popen`) rather than a one-shot script. `_start_rag_api()` launches `uvicorn main:app` in the `rag_api/` directory at startup; `_check_rag_api()` polls the process every 30 seconds and restarts it if it has exited; `_stop_rag_api()` terminates it gracefully (with `SIGKILL` fallback) on scheduler shutdown.
- **Ollama Python library** (`ollama.Client`) used for all LLM calls to `http://192.168.1.13:11434`; replaced raw `requests` calls in `ollama_client.py`
- **Tool-calling loop** (`chat_with_tools`) used by research and QA agents; tools passed as **Python callables** — the library auto-generates JSON schemas from type annotations. Web tools (`web_search`, `web_fetch`) are defined in `shared/web_search.py` and delegate to `ollama.web_search()` / `ollama.web_fetch()` from the ollama Python library; API key in `config.json → web_search.ollama_api_key`. **Critical:** `OLLAMA_API_KEY` env var must be set *before* `import ollama` runs — `ollama_client.py` handles this by setting the env var from config at import time, making it the first shared import in every agent script.
- **File watcher** (`scripts/shared/file_watcher.py`) — `TaskWatcher` (watchdog library) monitors `inbox/`, `validation/`, and all worker inboxes for `.task.md` events. When a file appears (or is modified), it coalesces bursts within 0.5s and triggers the relevant agent immediately via `scheduler.trigger_agent()`. Also scans for pre-existing files on startup. **Timer-based polling is optional** — controlled by `config.json → scheduler.enable_timer_polling` (currently `false`; agents are triggered exclusively by the file watcher). Requires `watchdog>=3.0.0` in `requirements.txt`; degrades gracefully if not installed.
- **Claude Code worker:** `subprocess.run(["claude", "--print", "-p", prompt])` — every prompt is prefixed with `_PIPELINE_PREAMBLE` (defined at the top of `agent_claude_code.py`) which instructs the CLI to respond via stdout only, preventing it from attempting filesystem writes or requesting permissions in non-interactive mode.
- **Task files** are `.task.md` with YAML frontmatter — see `ARCHITECTURE.md` for full schema
- **System prompts** stored in `agents/<name>/system_prompt.md` — edit to change agent behaviour without touching code. The orchestrator has two: `system_prompt.md` (decomposition) and `validation_system_prompt.md` (validation decisions)
- **Validation loop:** workers move completed tasks to `validation/` (not `outbox/`) via `mark_awaiting_validation()`; the orchestrator's Phase 1 reviews them and decides complete/refine/redo/additional_work. Max 5 iterations. The parent task stays in `processing/` throughout — it is only moved to `outbox/` when the orchestrator issues a `complete` decision. When the max-iteration cap is hit, the forced-completion path calls `write_result()` before `mark_completed()` (same as the normal complete branch) so `status: complete` and a `_result.md` summary are always written. On `complete`, the orchestrator sweeps ALL remaining tasks in `validation/` for that parent in two passes: (1) the known subtasks list from `get_completed_subtasks_by_parent`, then (2) a belt-and-suspenders glob of `validation/*.task.md` filtered by `parent_task_id` — catching any that slipped through the first pass. The stale-recovery path (parent already in outbox when orchestrator restarts) also uses `mark_completed()` rather than a bare `move_task()` for the same reason.
- **QA gate in validation:** for code subtasks with `chain_to: qa`, the orchestrator's validation phase will not fire until the QA task has completed (helper `_find_qa_for_coder_subtask` searches all pipeline folders). If QA is still in-flight (`agents/qa/inbox/` or `processing/`), the parent is skipped for that cycle. When QA FAIL on first attempt (`retry_count == 0`), QA itself dispatches a retry coder task — the orchestrator *waits* for that retry cycle to resolve (retry coder → QA2) before validating; it does not issue a second redo. `_find_retry_coder_output` uses a timestamp guard so it only matches retry coder tasks created *after* the specific QA task (preventing false positives from old completed tasks in `outbox/`). If the retry coder ends up in `failed/` (e.g. LLM timeout), `_RETRY_CODER_FAILED` sentinel is returned and the gate releases with QA1's verdict so the orchestrator LLM can decide. When QA finishes (PASS or `retry_count > 0`), its verdict and result are injected into the subtask context passed to the validation LLM.
- **Validation context propagation:** when the orchestrator issues a `redo`, `refine`, or `additional_work` decision, it passes a `validation_context` dict to the new follow-up subtasks. This dict contains the orchestrator's reasoning and any QA feedback from the previous iteration. Workers receive this as a `## Validation Context` section prepended to the task body so they understand exactly what failed or needs improvement — without this, retry and refinement subtasks had no awareness of prior attempts.
- **Validation result window:** in `agent_orchestrator.py`, `MAX_RESULT_CHARS = 256000` (~64k tokens at 4 chars/token) caps the result text passed to the validation LLM. When a result exceeds this limit, a `[TRUNCATED — showing first N of M chars ...]` note is appended so the LLM knows the full content is on disk. `validation_system_prompt.md` has a matching "Truncated Results" section that instructs the orchestrator to issue `complete` on well-formed output rather than requesting more work just because the preview is cut off.
- **Task dependencies:** coder tasks automatically get `depends_on: [research_task_id]` when research and code subtasks coexist; the orchestrator's Phase 2 wires the research result into `context_files` once complete, then unblocks the coder task. This dependency wiring is also applied to follow-up tasks created during `additional_work`/`refine`/`redo` iterations — so coder follow-ups always wait for research follow-ups to finish before running.
- **`redecompose_after_research`:** when the orchestrator's decomposition LLM decides research must happen before it can produce a good implementation plan, it can set `redecompose_after_research: true` on the parent task and dispatch only the research subtask(s) first. Once research completes and passes validation, `redecompose_with_research()` re-calls the decomposition LLM with the research result injected as context, producing the full set of implementation subtasks. An infinite-loop guard clears the flag immediately after the first re-decomposition so subsequent validation cycles are not re-triggered.
- **Orphan recovery:** four functions run at orchestrator startup in sequence:
  1. `recover_orphaned_tasks()` — scans `processing/` for tasks with `status: pending` (tasks whose decomposition never finished, e.g. killed mid-LLM-call) and moves them back to `inbox/` to be re-dispatched. `dispatched` and `processing` statuses are left alone.
  2. `recover_processing_subtasks()` — scans `processing/` for non-orchestrator subtasks with `status: processing` older than 720 seconds (12 min). These are workers killed mid-LLM-call after `mark_processing()` but before finishing. Resets `status` to `pending` and returns them to the appropriate worker inbox. Time-based detection (720s > any realistic LLM call including max tool turns). Does NOT increment `stall_retry_count` — this is infrastructure failure recovery, not task-content failure.
  3. `recover_stalled_subtasks()` — scans `failed/` for subtask files whose parent is still in `processing/`. Groups by parent. If `stall_retry_count < 2`, resets the subtasks and returns them to the worker inbox, incrementing `stall_retry_count` on the parent. If max retries exhausted, writes a failure report and moves the parent to `failed/`. Subtasks whose parent is already in `outbox/` are silently skipped.
  4. `recover_orphaned_validation_subtasks()` — sweeps `validation/` for stranded subtasks (QA tasks and retry coders created before `parent_task_id` propagation, or subtasks whose parent completed just before a restart). Two detection cases: (a) `parent_task_id` points to a `status: complete` parent in `outbox/`; (b) no `parent_task_id` but `output_path` result file already exists in `outbox/`. Both cases call `mark_completed()` to move the subtask to `outbox/`.
- **`parent_task_id` propagation:** coder stamps `parent_task_id` on the QA task it creates via `chain_to: qa`; QA stamps `parent_task_id` on the retry coder task it creates on FAIL. This propagates the parent linkage through the full coder → QA → retry coder → retry QA chain, ensuring all generated subtasks are visible to `get_completed_subtasks_by_parent` and are swept to `outbox/` when the parent closes. Without this, QA tasks and retry coders had no `parent_task_id` and were left stranded in `validation/` after parent completion.
- **SIGINT isolation:** agent subprocesses are spawned with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` (Windows) or `start_new_session=True` (Unix) so a Ctrl+C in the scheduler terminal does not propagate to agents mid-LLM-call, preventing task orphaning.
- **Startup health checks:** before spawning any agents, the scheduler (a) flushes all `__pycache__` directories under `scripts/` so agents always import fresh bytecode, (b) test-imports `shared/task_io.py` — if it fails to import, the scheduler logs a FATAL error and aborts without starting any agents, and (c) initialises the file watcher (`TaskWatcher`) for all task folders.
- **QA feedback:** the `FEEDBACK:` block in QA's LLM response is captured in full (multi-line) using `re.DOTALL`, so retry tasks receive complete actionable feedback rather than a truncated first line.
- **QA empty-response retry:** if Ollama returns an empty or whitespace-only response, QA retries the LLM call up to 2 times before defaulting to FAIL. The retry condition checks `response is None or response.strip() == ""` (not a simple truthiness check, which would miss empty strings).
- **Task ID uniqueness:** IDs include microseconds (`task_YYYYMMDD_HHMMSS_microseconds`) to prevent collisions when subtasks are created in the same second
- **Approval gate for claude-code:** orchestrator routes to `pending_approval` which places tasks in `agents/claude-code/pending/`; approve or reject from the dashboard **Approvals** tab (or manually move files). The dispatch validator accepts both `'pending_approval'` and `'claude-code'` as valid worker strings. The `approve_task` function in `task_monitor.py` writes the approved file with a leading `---` delimiter (`f"---\n{frontmatter}\n---\n{body}"`) so `python-frontmatter` can detect the YAML block — omitting this delimiter caused `id` to be read as `"unknown"` (the N2 bug).
- **Worker task status:** `mark_processing()` writes `status: processing` into the task frontmatter before moving it to `processing/`. This prevents the orphan recovery loop from re-dispatching actively running subtasks (which would previously match on `status: pending`). Uses a string-based regex replacement (not a `python-frontmatter` round-trip) to guarantee all original frontmatter fields (id, output_path, parent_task_id, etc.) are preserved — the round-trip approach was the N2 bug that caused `agent_claude_code.py` to read `id` as `"unknown"` and write results to `unknown_result.md`.
- **`context_files` wiring:** when the orchestrator's dependency resolution phase unblocks a coder task, it reads the completed research task's `output_path` from its metadata in `outbox/` and injects the path into the coder task's `context_files` list. The coder therefore always receives research findings as context.
- **`write_result` import:** imported at module level in `agent_orchestrator.py` (not inside a conditional block) so the validation decision handler can always reference it, preventing `UnboundLocalError` and ensuring parent task `status: complete` is persisted before `mark_completed()` moves the file to `outbox/`. This fixes the History tab displaying completed tasks.
- **UTF-8 encoding:** all `open()` calls across every agent script and `shared/task_io.py` use `encoding='utf-8'`. This prevents the Windows default codec (`cp1252`) from crashing on UTF-8 characters (e.g. non-ASCII chars in research output written as context files for the coder).
- **Task file parsing (dashboard):** `dashboard/task_monitor.py` uses a hand-rolled YAML parser (`_parse_yaml_frontmatter`) that splits on the first `:` and preserves backslashes, avoiding `yaml.safe_load` failures on Windows paths like `C:\Users\...`. The task monitor scans all pipeline folders including `validation/` (`get_all_tasks`, `get_task_detail`, `get_task_payload` all include this folder). Worker result files are identified by scanning `outbox/*_result.md` for files whose embedded `agent:` metadata matches — since worker `.task.md` files stay in `validation/`, not `outbox/`, this is the only reliable source of worker results.
- **Dashboard logs tab:** entries are rendered newest-first (`[...data.logs].reverse()`) and the container scrolls to the top on update, so the latest line is always immediately visible.
- **Dashboard Approvals modal:** `dashboard.js` keeps an `approvalsCache` object (keyed by task ID) that is populated on every `updateApprovals()` call. `showApprovalDetail()` reads directly from this cache — there is no `/api/pending-approvals/<id>` endpoint, so the previous `fetch` call returned an error object and all metadata fields showed as `undefined`.
- **Coder import checklist:** `agents/coder/system_prompt.md` includes a mandatory "Import Checklist" section listing every commonly-forgotten stdlib module (`sys`, `os`, `re`, `json`, `argparse`, `pathlib`, etc.) with examples of what each is needed for, plus a final scan instruction. This directly targets the `import sys` omission that caused all T2 QA failures in v7.
- **Token logging:** after every Ollama call, each agent appends `{ts, task_id, prompt, completion}` to `logs/<agent>/tokens.jsonl` via `scripts/shared/token_logger.py`; the dashboard Agent Stats tab shows cumulative totals
- **Concurrency guard:** orchestrator uses a lockfile (`processing/orchestrator.lock`) with PID validation
- **Scheduler** is a Python threading-based loop (`scripts/scheduler.py`), not cron — works on Windows
- **Config** centralized in `config.json`; loaded via `scripts/shared/config.py` (`ProjectConfig` class); Ollama timeout is `360s`. Each agent also has a `process_timeout` (scheduler-level kill ceiling): orchestrator 600s, coder 600s, research 1800s, qa 1200s, claude-code 600s. `process_timeout` must exceed `ollama_timeout × max_tool_turns` to avoid killing an agent mid-loop.
- **Dashboard** is a separate Flask process (`dashboard/app.py`); reads directly from the shared filesystem — no DB required; tasks can be submitted from the **Submit Task** tab; results browsable by agent in the **Results** tab
- **Log timestamps** use `datetime.fromtimestamp(time.time(), tz=timezone.utc)` — correct UTC on Windows
- **Test suite** lives under `tests/` (pytest). Run with `pytest` after `pip install -r requirements-dev.txt`. The `fake_project` fixture in `tests/conftest.py` builds a temp pipeline tree and monkey-patches `PROJECT_ROOT` in `shared.task_io`, `shared.token_logger`, and (per-test) `agent_orchestrator`, so tests never touch the real `inbox/`/`outbox/`. Network is mocked in `test_rag_tool.py` (`requests`) and `test_ollama_client.py` (`_ollama.Client`). Current coverage on `scripts/shared/` is ~82% weighted (config/rag_tool/token_logger 100%, logger 97%, ollama_client 94%, task_io 91%); `file_watcher.py` and `web_search.py` are deferred. The orchestrator's pure helpers (`_find_qa_for_output`, `_find_retry_coder_output`, `_find_qa_for_coder_subtask`, `_extract_qa_verdict`) are covered; LLM-driven paths (decomposition, validation, recovery) need full Ollama mocking and are out of scope for the initial suite. See `ARCHITECTURE.md → Testing` for fixtures and conventions when adding new tests.
- **Module-level logger in `agent_orchestrator.py`:** the `_find_*` helpers are not passed an `AgentLogger`, so they use `_module_log = logging.getLogger(__name__)` (defined at top of file) for `log.debug()` calls inside narrowed `except` blocks. Calling `log.<level>` (without `_module_log`) inside these helpers will `NameError` — `log` only exists as a local in `main()`.

## Folder Structure

```
AI Team/
  CLAUDE.md                              ← you are here
  ARCHITECTURE.md                        ← full design doc
  DASHBOARD.md                           ← dashboard usage and API reference
  config.json                            ← centralized config (includes rag_api.url)
  RUN_SCHEDULER.bat / RUN_SCHEDULER.sh   ← quick-start scripts
  requirements.txt
  requirements-dev.txt     ← pytest, pytest-cov
  pytest.ini               ← pytest config (testpaths=tests)
  tests/                   ← pytest suite — see ARCHITECTURE.md → Testing
    conftest.py            ← fake_project fixture (re-points PROJECT_ROOT)
    test_task_io.py        ← frontmatter, mark_processing, safe_read_context
    test_rag_tool.py       ← graceful-degradation matrix
    test_rag_injection.py  ← pre-prompt injection helper (M2)
    test_config.py         ← ProjectConfig accessors + JSON loader
    test_logger.py         ← AgentLogger levels, UTF-8, append
    test_token_logger.py   ← tokens.jsonl, task-ID filter
    test_ollama_client.py  ← chat()/chat_with_tools() with mocked Ollama
    test_orchestrator_helpers.py ← QA chain discovery, verdict extraction
  inbox/                   ← drop .task.md files here to submit work
  processing/              ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/              ← completed subtasks awaiting orchestrator approval
  outbox/                  ← approved & completed results
  failed/                  ← QA failure reports + hard-errored tasks
  context/                 ← optional shared context files for tasks
  rag_api/                 ← Local knowledge base (FastAPI + ChromaDB)
    main.py                ← FastAPI app; endpoints: /health /ingest /query /documents
    config.py              ← Settings class (plain os.getenv, NOT pydantic_settings)
    ollama_client.py       ← OllamaClient: embed() via /api/embeddings; rerank() via cosine sim
    vector_store.py        ← ChromaDBPersistentClient wrapper
    ingestion.py           ← TextChunker + DocumentLoader
    models.py              ← Pydantic v2 request/response models
    requirements.txt       ← fastapi, uvicorn, chromadb, pydantic, requests
    chroma_db/             ← ChromaDB persistent storage (auto-created)
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
      web_search.py       ← web_search() and web_fetch() tool wrappers
      rag_tool.py         ← rag_query(query, top_k) → str tool wrapper for the RAG API
      rag_injection.py    ← inject_rag_context() — pre-prompt RAG for non-tool-loop agents
      logger.py           ← UTC-correct logger
      config.py           ← config.json loader (includes rag_api_url() method)
    agent_orchestrator.py
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py
```

## Running the System

**Terminal 1 — Agents + RAG API:**
```
RUN_SCHEDULER.bat        (Windows)
RUN_SCHEDULER.sh         (Linux/Mac)
```
Or manually: `python scripts/scheduler.py`

The scheduler automatically starts the RAG API (`uvicorn` at `http://localhost:8000`) and keeps it alive. Logs: `logs/scheduler/general.log` and `logs/<agent>/general.log`. Press Ctrl+C to stop all agents and the RAG API.

**RAG API prerequisites** (first time only):
```bash
cd rag_api
pip install -r requirements.txt
```

**Terminal 2 — Dashboard (optional):**
```bash
python dashboard/run_dashboard.py
```
Open `http://localhost:5000`. Runs independently of the scheduler. The **Knowledge Base** tab lets you add, view, and delete documents from the RAG API without touching the filesystem.

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
5. **RAG auto-ingestion** — automatically ingest completed task results and context files into the knowledge base so agents accumulate project knowledge over time without manual uploads
6. **RAG dashboard search** — add a query box to the Knowledge Base tab so João can search the vector store from the browser
