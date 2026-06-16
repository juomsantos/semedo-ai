# AI Team — Agent Coordination System

Multi-agent AI system using a shared filesystem as the message bus. Agents are triggered immediately by a file watcher; timer-based polling is an optional fallback. See `ARCHITECTURE.md` for the full design and `DASHBOARD.md` for dashboard/API reference.

---

## What's Running

| Component | Model / Tech | Role |
|---|---|---|
| Orchestrator | qwen3.5:9b | Decomposes inbox tasks, validates subtask results, resolves dependencies, routes work |
| Coder | qwen3.5:9b | Code generation; RAG pre-prompt injection before each task |
| Research | qwen3.5:9b | Research, analysis, Q&A; tool loop: web_search + web_fetch + rag_query |
| Claude Code | claude CLI | Complex reasoning tasks; requires manual dashboard approval before running |
| QA | qwen3.5:9b | Code review + execution testing; tool loop: web_search + web_fetch + rag_query; one auto-retry on FAIL |
| RAG API | FastAPI + ChromaDB | Local knowledge base at `http://localhost:8000`; started and monitored by the scheduler |
| Dashboard | Flask | Real-time web UI at `http://localhost:5000`; independent process |

**Ollama server:** `http://192.168.1.13:11434` (configurable in `config.json`)

---

## Key Technical Notes

**RAG injection — two modes:**
- *Tool mode* (research, QA): `rag_query` is in the `TOOLS` list; the model decides when to call it (up to 5 turns within the shared `MAX_TOOL_TURNS` ceiling).
- *Pre-prompt mode* (coder, orchestrator): `inject_rag_context()` in `shared/rag_injection.py` queries the RAG API on the first 500 chars of the task body and prepends a `## Knowledge Base Context` block before the LLM call. Unavailable/empty results are silently filtered — the task body is returned unchanged if the API is down.

**OLLAMA_API_KEY must be set before `import ollama` runs.** `ollama_client.py` does this at import time from `config.json`. It is always the first shared import in every agent script.

**`context_files` paths must be Windows paths.** The coder calls `Path(cf).exists()` on Windows and silently skips anything that doesn't resolve. Always read `output_path` from a task's own frontmatter — never use `Path(...).resolve()` from bash (produces Linux mount paths the coder can't open).

**`mark_processing()` uses regex, not a frontmatter round-trip.** A round-trip via `python-frontmatter` drops fields silently. The regex replacement guarantees all original frontmatter fields survive (id, output_path, parent_task_id, etc.). Both `mark_processing()` and `mark_awaiting_validation()` pass `count=1` to `re.sub()` to avoid replacing multiple `status:` occurrences if a malformed frontmatter block contains duplicates.

**Dashboard YAML parser** (`task_monitor.py::_parse_yaml_frontmatter`) uses `yaml.safe_load`. PyYAML handles unquoted Windows backslash paths correctly. Malformed YAML returns `{}`.

**Dashboard auth:** state-changing endpoints are gated by an `X-Dashboard-Token` header. The token is generated at startup (or read from `$DASHBOARD_TOKEN`), injected into the served HTML as `<meta name="dashboard-token">`, and attached by `dashboard.js` to every POST/DELETE via `withAuth()`. After a dashboard restart, browser tabs need a refresh. Set `$DASHBOARD_TOKEN` for stability across restarts.

**Dashboard endpoints all return JSON errors.** Every Flask endpoint is wrapped with `@_json_error_envelope` — unhandled exceptions return `{"error": "..."}` with an appropriate status code rather than an HTML 500 page.

**Dashboard chat markdown:** assistant responses are rendered as HTML via `marked.js` (GFM mode). Code blocks are syntax-highlighted by `highlight.js` via `hljs.highlightElement()` applied post-DOM-insert. User messages are plain text.

**Dashboard chat streaming:** chat uses `POST /api/chat/stream` (SSE), not the blocking `/api/chat`. The browser reads `text/event-stream` via `ReadableStream`; each `data: {json}` line carries one of five event types: `meta` (session id), `tool_call` (tool dispatched), `thinking` (reasoning chunk), `token` (content chunk), `done` (full assembled text + optional `action`). `stream_chat_with_tools()` in `agent_chat.py` is a generator that drives the whole conversation through `client.stream_with_tools()` — **one streaming LLM call per turn**. Each turn streams `thinking`/`token` events live and accumulates any `tool_calls`; if a turn produced tool calls they are dispatched (emitting `tool_call` events) and the loop streams the next turn, otherwise that streamed text is the final answer and a `done` is emitted. There is no separate non-streaming probe call and no discarded generation (the old two-phase design that re-called `chat_with_tools()` then `stream_response()` is gone). At the `max_tool_turns` limit a final answer is forced with `tools=[]`. `/api/chat/stream` is **not** wrapped in `@_json_error_envelope` — errors are delivered as `{"type":"error",...}` SSE events.

**Dashboard chat models + thinking mode:** chat is **multi-model**. `config.json → chat.models` is an array of model objects (`name`, `label`, `is_default`, `options_standard`, `options_thinking`); `app.py` parses it into `CHAT_MODELS` and exposes them via `GET /api/models`, with the `is_default` entry as the initial selection (currently `gemma-4-12B`). The request body's `model` field picks which model to use per call; `_get_model_config(model_name)` returns that model's two option sets (falling back to `DEFAULT_CHAT_OPTIONS_*` for an unknown name). The old single-model `chat.model` / top-level `chat.options_standard/options_thinking` layout is still accepted as a backward-compatibility fallback when `chat.models` is absent. A toggle (⚡ Standard / 🧠 Thinking) sends `thinking_mode: true/false`; the backend selects the active model's `options_thinking` or `options_standard` and passes `think=thinking_mode` to Ollama. When thinking content arrives in stream chunks (`chunk.message.thinking`), `stream_with_tools()` yields it as `{"thinking": str, "content": "", "tool_calls": [], "done": false}`; the browser renders it in a collapsible `<details>` block.

**Dashboard Ollama API logging:** `dashboard/ollama_api_logger.py` (`OllamaAPILogger`) appends every chat LLM request/response/stream-chunk/error to `logs/dashboard/ollama_api.jsonl` as one JSON line each, serialized through a module-level `_WRITE_LOCK` so the `app.py` and `agent_chat.py` instances never interleave. It uses open-append-close (no persistent handle) so `clear_logs()` can always truncate/remove the file on Windows. `read_logs(limit=N)` tails by **request group** (one LLM call + all its chunks), not raw lines, so a streaming call's hundreds of chunk lines can't evict the originating `request` line. Surfaced in the dashboard logs dropdown.

**Validation loop mechanics:**
- Workers move completed tasks to `validation/` via `mark_awaiting_validation()` — never directly to `outbox/`.
- Orchestrator Phase 1 reviews each parent's subtasks and returns `complete | refine | redo | additional_work`. Max 5 iterations; forced `complete` at the cap.
- On `complete`, the orchestrator sweeps all remaining subtasks for that parent out of `validation/` in two passes (known list + glob fallback) before closing.
- **File extraction on `complete`:** after closing the parent, every passing code subtask's result body is scanned for `**path/to/file.ext**\n```lang\n...\n```\n` blocks (`_extract_named_files` in `orchestration/validate.py`). Each named file is written to `outputs/<parent_task_id>/<rel_path>`. Retry coders (`created_by == "qa"`) supersede their originals — the link is `context_files[0]` on the retry equals the original's `output_path`. Multiple distinct subtasks are all extracted independently. Absolute paths and `..` traversal in LLM-generated filenames are rejected by `_is_safe_output_path`. Extraction errors are logged and never block task completion.
- `MAX_RESULT_CHARS = 256000` caps what's passed to the validation LLM; truncated results get a `[TRUNCATED]` note so the LLM doesn't request more work just because the preview is cut.
- **Validation repair call:** if the validation LLM returns unparseable JSON, a single repair attempt is made using `threading.Thread(daemon=True)` with a hard 300s wall-clock ceiling (`t.join(timeout=300)`). The repair call receives the **full original context** (parent task + all subtask results, identical to the first call) plus a note about the parse failure — not just a fragment of the bad response. If the repair thread is still alive after 300s, `_VALIDATION_PARSE_FAILED` is returned and the parent is moved to `failed/`. **Do not use `ThreadPoolExecutor` here** — its `with` block calls `shutdown(wait=True)` on `__exit__`, which blocks until the Ollama thread finishes regardless of `future.result(timeout=N)`, completely negating the timeout.

**QA gate:** the orchestrator's Phase 1 does not fire on a code subtask until its QA task has finished. If QA is still in-flight, the parent is skipped that cycle. On first FAIL (`retry_count == 0`), QA dispatches a retry coder task; the orchestrator waits for the full retry cycle (retry coder → QA2) before validating. `_find_retry_coder_output` uses a timestamp guard to avoid matching old completed tasks. If the retry coder ends up in `failed/`, a sentinel is returned and Phase 1 uses QA1's verdict.

**Validation context:** on `redo`/`refine`/`additional_work`, a `## Validation Context` block (from `shared/validation_context.py::prepend_validation_context`) is prepended to follow-up task bodies. Two injection points: `task_io.create_task_file` (covers coder/research/claude-code, which read `task["body"]` directly) and `agent_qa.review_with_llm` (QA builds its prompt from `original_description`, so it re-injects explicitly). The `original_description` field is kept free of validation context so the coder→QA chain doesn't double-inject.

**`parent_task_id` propagation:** coder stamps it on the QA task; QA stamps it on any retry coder. This chains the full coder → QA → retry coder → retry QA linkage so all generated subtasks are swept to `outbox/` when the parent closes.

**`redecompose_after_research`:** if the decomposition LLM decides it needs research output before producing a good plan, it sets this flag and dispatches only the research subtask. After research passes validation, `redecompose_with_research()` re-calls the decomposition LLM with research results injected, then dispatches the full plan. The flag is cleared immediately after re-decomposition.

**Orphan recovery** (runs at orchestrator startup, in `orchestration/recovery.py`):
1. `recover_orphaned_tasks()` — returns `status: pending` parent tasks in `processing/` to `inbox/`.
2. `recover_processing_subtasks()` — returns subtasks stuck in `status: processing` for >720s to their worker inbox (workers killed mid-LLM-call). Does not increment `stall_retry_count`. Covers all four workers: `coder`, `research`, `claude-code`, and `qa` (Fix 1: `"qa"` added to `WORKER_INBOXES` in `agent_orchestrator.py`).
3. `recover_stalled_subtasks()` — retries subtasks in `failed/` whose parent is still in `processing/` (up to `stall_retry_count < 2`); writes failure report and moves parent to `failed/` at the limit.
4. `recover_orphaned_validation_subtasks()` — sweeps `validation/` for subtasks whose parent completed before a restart.

**Agent error handling — three patterns:**
1. *Startup pre-flight*: `main()` checks external dependency (Ollama / claude CLI); `sys.exit(1)` on failure.
2. *Critical-path failure*: `OllamaError` on the per-task LLM call → `mark_failed(task_path)` (workers) or return sentinel (orchestrator phases).
3. *Outer loop guard*: the `for task_path in tasks:` loop wraps `process_task` in `try/except Exception` so one corrupt task doesn't kill the cycle.

**Agent boilerplate** (`shared/agent_boilerplate.py`): `load_system_prompt`, `build_user_message(task, *, style)`, `log_tokens_safe`. Three styles — `"coder"`, `"research"`, `"claude-code"` — produce different context-file rendering formats. QA does not use `build_user_message` because it builds its prompt from `original_description`, not `task["body"]`.

**Orchestrator package** (`scripts/orchestration/`): `decompose.py`, `validate.py`, `dispatch.py`, `recovery.py`, `qa_chain.py`, `parsing.py`. `agent_orchestrator.py` is a thin entrypoint. Helpers inside the sub-modules use `_module_log = logging.getLogger(__name__)` — not `log`, which only exists as a local in `main()`.

**Token logging:** Ollama-backed agents append `{ts, task_id, prompt, completion}` to `logs/<agent>/tokens.jsonl`. The `claude-code` worker uses a word-count proxy for completion tokens and logs `0` for prompt tokens (the Claude CLI does not report counts).

**SIGINT isolation:** agent subprocesses use `CREATE_NEW_PROCESS_GROUP` (Windows) or `start_new_session=True` (Unix) so Ctrl+C in the scheduler terminal doesn't propagate to in-flight LLM calls.

**Scheduler startup sequence:** (1) flush `__pycache__` under `scripts/`; (2) test-import `shared/task_io.py` — abort on failure; (3) start RAG API subprocess; (4) init file watchers; (5) start scheduling loop.

**Config:** `config.json` → `scripts/shared/config.py` (`ProjectConfig`). Key fields: `ollama.timeout` (360s per call), `agents.<name>.process_timeout` (scheduler kill ceiling — must exceed `timeout × max_tool_turns`), `agents.<name>.options` (passed verbatim to `ollama.Client.chat`), `agents.<name>.thinking` (null = library default), `scheduler.enable_timer_polling` (currently false), `rag_api.url`. The dashboard chat reads `chat.timeout`, `chat.max_tool_turns`, `chat.max_history_turns`, and the `chat.models` array (per-model `options_standard`/`options_thinking`); `app.py` consumes `chat.models` directly rather than through `ProjectConfig` accessors.

**Dependencies:** pinned to `==<version>` in `requirements.txt` / `rag_api/requirements.txt`. Full transitive closure in `requirements.lock`. To upgrade: bump version, `pip install -r requirements.txt --upgrade`, then `python scripts/_gen_locks.py`, run tests, commit.

---

## Folder Structure

```
AI Team/
  CLAUDE.md                              ← you are here
  ARCHITECTURE.md                        ← full design doc
  DASHBOARD.md                           ← dashboard usage and API reference
  config.json                            ← centralized runtime config
  RUN_SCHEDULER.bat / RUN_SCHEDULER.sh   ← quick-start scripts
  requirements.txt / requirements.lock   ← pinned deps + transitive closure
  requirements-dev.txt                   ← pytest, pytest-cov
  pytest.ini
  tests/
    conftest.py                          ← fake_project fixture, sample_task_meta
    test_task_io.py                      ← frontmatter round-trip, mark_processing, safe_read_context
    test_rag_tool.py                     ← graceful-degradation across all failure modes
    test_rag_injection.py                ← pre-prompt injection helper
    test_agent_error_handling.py         ← static AST checks: pre-flight, OllamaError, outer loop guard
    test_config.py                       ← ProjectConfig accessors + JSON loader
    test_logger.py                       ← AgentLogger levels, UTC timestamps, UTF-8
    test_token_logger.py                 ← tokens.jsonl output, task-ID filter
    test_ollama_client.py                ← chat() + chat_with_tools() with mocked Ollama
    test_orchestrator_helpers.py         ← QA chain discovery, verdict extraction
    test_agent_boilerplate.py            ← build_user_message parity matrix, log_tokens_safe
    test_dashboard_token.py              ← X-Dashboard-Token guard on state-changing endpoints
    test_task_monitor.py                 ← YAML frontmatter parser, approve/reject flows
    test_validation_context_propagation.py ← prepend_validation_context, QA injection guard
  inbox/                   ← drop .task.md files here to submit work
  processing/              ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/              ← completed subtasks awaiting orchestrator review
  outbox/                  ← completed results
  outputs/                 ← named source files extracted from completed coder tasks; outputs/<parent_task_id>/<rel_path>
  failed/                  ← QA failure reports + hard-errored tasks
  context/                 ← optional shared context files
  rag_api/
    main.py                ← FastAPI: /health /ingest /query /documents /documents/<id>
    config.py              ← plain Settings class; _KNOWN_KEYS warns on config typos
    ollama_client.py       ← embed() via /api/embeddings; rerank() via cosine similarity
    vector_store.py        ← ChromaDB wrapper (get()→flat lists, query()→nested lists)
    ingestion.py           ← TextChunker + DocumentLoader
    models.py              ← Pydantic v2 models
    requirements.txt
    chroma_db/             ← persistent vector store (auto-created)
  agents/
    orchestrator/
      system_prompt.md             ← decomposition & routing prompt
      validation_system_prompt.md  ← validation decision prompt
    coder/inbox/ + system_prompt.md
    research/inbox/ + system_prompt.md
    claude-code/
      inbox/              ← approved tasks ready to run
      pending/            ← awaiting manual approval
    qa/inbox/ + system_prompt.md
  dashboard/
    app.py                ← Flask REST API + @_json_error_envelope on all endpoints
    run_dashboard.py      ← launcher
    task_monitor.py       ← filesystem scanner; yaml.safe_load for frontmatter
    agent_chat.py         ← chat LLM tool loop: call_chat_with_tools (blocking) + stream_chat_with_tools (SSE generator)
    ollama_api_logger.py  ← OllamaAPILogger: appends chat LLM traffic to logs/dashboard/ollama_api.jsonl
    chat_context.py       ← pipeline snapshot + deep task context injector
    chat_session.py       ← in-memory UUID-keyed sessions (max 20 history turns)
    chat_system_prompt.md
    templates/index.html
    static/dashboard.js / dashboard.css
  logs/                   ← logs/<agent>/general.log + tokens.jsonl
  scripts/
    shared/
      task_io.py          ← task file I/O, dependency resolution, validation grouping
      ollama_client.py    ← chat() + chat_with_tools() + stream_with_tools(); sets OLLAMA_API_KEY at import time
      token_logger.py     ← appends {ts, task_id, prompt, completion} to tokens.jsonl
      web_search.py       ← web_search() + web_fetch() wrappers (ollama Python library)
      rag_tool.py         ← rag_query(query, top_k) → str; graceful fallback on unavailability
      rag_injection.py    ← inject_rag_context() — pre-prompt RAG for coder + orchestrator
      agent_boilerplate.py ← load_system_prompt, build_user_message (3 styles), log_tokens_safe
      validation_context.py ← prepend_validation_context() — ## Validation Context blocks
      file_watcher.py     ← TaskWatcher: watchdog-based immediate agent triggering
      logger.py           ← UTC-correct file + stdout logger
      config.py           ← ProjectConfig: loads config.json, exposes typed accessors
    agent_orchestrator.py ← thin entrypoint; delegates to orchestration/ package
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py
    orchestration/
      decompose.py        ← decomposition LLM + redecompose_after_research
      validate.py         ← Phase 1: validation loop + QA gate; file extraction to outputs/ on complete
      dispatch.py         ← Phase 3: task routing + subtask creation
      recovery.py         ← four startup orphan/stall recovery functions
      qa_chain.py         ← _find_qa_for_coder_subtask, _find_retry_coder_output, _extract_qa_verdict
      parsing.py          ← LLM response parsing helpers
```

---

## Running the System

**Terminal 1 — Agents + RAG API:**
```
RUN_SCHEDULER.bat        (Windows)
RUN_SCHEDULER.sh         (Linux/Mac)
```
Or: `python scripts/scheduler.py`

The scheduler starts and monitors the RAG API automatically. Press Ctrl+C to stop everything.

**First-time RAG API setup:**
```bash
cd rag_api && pip install -r requirements.txt
```

**Terminal 2 — Dashboard (optional):**
```bash
python dashboard/run_dashboard.py   # http://localhost:5000
```

---

## Submitting a Task

**Via dashboard:** `http://localhost:5000` → **Submit Task** tab.

**Via file drop** — write a `.task.md` to `inbox/`:

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

The orchestrator picks it up immediately (file watcher) or within the timer interval, decomposes it, and routes subtasks to workers.

---

## Monitoring

- **Dashboard:** `http://localhost:5000` — task status, agent stats, logs, claude-code approvals
- **Task flow:** `inbox/` → `processing/` → `agents/*/inbox/` → `validation/` → `outbox/` (or `failed/`)
- **Logs:** `logs/<agent>/general.log` | token usage: `logs/<agent>/tokens.jsonl`
- **Claude-code approvals:** dashboard **Approvals** tab, or manually move files from `agents/claude-code/pending/` → `agents/claude-code/inbox/`

---

## Potential Extensions

1. **Worker-initiated research** — allow coder/QA to drop tasks in `research/inbox/` mid-execution and yield until resolved
2. **Webhooks** — notify external systems when tasks complete
3. **RAG auto-ingestion** — automatically ingest completed task results into the knowledge base
4. **RAG dashboard search** — query box in the Knowledge Base tab for ad-hoc vector searches
