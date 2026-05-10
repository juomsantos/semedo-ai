# AI Team — Agent Coordination System

This project is a multi-agent AI coordination system. Agents communicate through a shared filesystem, polled on a schedule via `scripts/scheduler.py`. A real-time web dashboard is available at `http://localhost:5000`. See `ARCHITECTURE.md` for the full design and `DASHBOARD.md` for dashboard usage.

## Current Status: Fully Implemented ✓

All agents are built and running with a continuous **orchestrator validation loop**: completed subtask results flow into `validation/`, the orchestrator reviews them, and decides whether to accept, refine, or request more work — up to 5 iterations before forcing completion.

## What's Running

A three-tier multi-agent system:

1. **Claude (Cowork)** — master coordinator, writes tasks to `inbox/`
2. **Orchestrator** (`qwen3.5:9b`) — polls `inbox/` every 3 min, runs 3 phases per cycle: validate completed work → resolve task dependencies → decompose and dispatch new tasks
3. **Workers:**
   - `qwen2.5-coder:7b` (coder) — code generation, polls every 2 min; skips tasks with unresolved dependencies
   - `qwen3.5:9b` (research) — research, summarization, Q&A, polls every 2 min; live web search via DuckDuckGo (up to 5 searches/task)
   - `claude CLI` (claude-code) — complex/reasoning tasks, polls every 3 min; tasks require manual approval first (land in `agents/claude-code/pending/` before `inbox/`)
   - `qwen3.5:9b` (qa) — code review + execution testing, polls every 2 min; live web search for error lookup (up to 3 searches/task)

4. **Dashboard** (`Flask`) — real-time web UI at `http://localhost:5000`; start with `python dashboard/run_dashboard.py`

## Key Technical Decisions

- **Ollama REST API** at `http://192.168.1.13:11434/api/chat`, `stream: false`
- **Tool-calling loop** (`chat_with_tools`) used by both research and QA agents for DuckDuckGo web search
- **Claude Code worker:** `subprocess.run(["claude", "--print", "-p", prompt])` — every prompt is prefixed with `_PIPELINE_PREAMBLE` (defined at the top of `agent_claude_code.py`) which instructs the CLI to respond via stdout only, preventing it from attempting filesystem writes or requesting permissions in non-interactive mode.
- **Task files** are `.task.md` with YAML frontmatter — see `ARCHITECTURE.md` for full schema
- **System prompts** stored in `agents/<name>/system_prompt.md` — edit to change agent behaviour without touching code. The orchestrator has two: `system_prompt.md` (decomposition) and `validation_system_prompt.md` (validation decisions)
- **Validation loop:** workers move completed tasks to `validation/` (not `outbox/`) via `mark_awaiting_validation()`; the orchestrator's Phase 1 reviews them and decides complete/refine/redo/additional_work. Max 5 iterations. The parent task stays in `processing/` throughout — it is only moved to `outbox/` when the orchestrator issues a `complete` decision. When the max-iteration cap is hit, the forced-completion path calls `write_result()` before `mark_completed()` (same as the normal complete branch) so `status: complete` and a `_result.md` summary are always written. On `complete`, the orchestrator also calls `mark_completed()` on every subtask in `validation/` for that parent — so subtasks land in `outbox/` with `status: complete` in the same cycle, not `awaiting_validation`. The stale-recovery path (parent already in outbox when orchestrator restarts) also uses `mark_completed()` rather than a bare `move_task()` for the same reason.
- **QA gate in validation:** for code subtasks with `chain_to: qa`, the orchestrator's validation phase will not fire until the QA task has completed (helper `_find_qa_for_coder_subtask` searches all pipeline folders). If QA is still in-flight (`agents/qa/inbox/` or `processing/`), the parent is skipped for that cycle. When QA FAIL on first attempt (`retry_count == 0`), QA itself dispatches a retry coder task — the orchestrator *waits* for that retry cycle to resolve (retry coder → QA2) before validating; it does not issue a second redo. `_find_retry_coder_output` uses a timestamp guard so it only matches retry coder tasks created *after* the specific QA task (preventing false positives from old completed tasks in `outbox/`). If the retry coder ends up in `failed/` (e.g. LLM timeout), `_RETRY_CODER_FAILED` sentinel is returned and the gate releases with QA1's verdict so the orchestrator LLM can decide. When QA finishes (PASS or `retry_count > 0`), its verdict and result are injected into the subtask context passed to the validation LLM.
- **Validation result window:** in `agent_orchestrator.py`, `MAX_RESULT_CHARS = 256000` (~64k tokens at 4 chars/token) caps the result text passed to the validation LLM. When a result exceeds this limit, a `[TRUNCATED — showing first N of M chars ...]` note is appended so the LLM knows the full content is on disk. `validation_system_prompt.md` has a matching "Truncated Results" section that instructs the orchestrator to issue `complete` on well-formed output rather than requesting more work just because the preview is cut off.
- **Task dependencies:** coder tasks automatically get `depends_on: [research_task_id]` when research and code subtasks coexist; the orchestrator's Phase 2 wires the research result into `context_files` once complete, then unblocks the coder task. This dependency wiring is also applied to follow-up tasks created during `additional_work`/`refine`/`redo` iterations — so coder follow-ups always wait for research follow-ups to finish before running.
- **Orphan recovery:** on every startup, the orchestrator scans `processing/` for tasks with `status: pending` (tasks it started decomposing but never finished, e.g. killed mid-LLM-call) and moves them back to `inbox/` to be re-dispatched. After a parent task is successfully dispatched (subtasks created), its status is updated to `dispatched` — orphan recovery only touches `pending` tasks, so `dispatched` parents are left in `processing/` until the validation loop completes them. Subtasks in `validation/` whose parent is gone from `processing/` are handled as follows: if the parent is found in `outbox/` with `status: complete`, the subtask is moved to `outbox/` (not `failed/`) — this prevents a scheduler restart from polluting the Failed count with legitimate completed work. If the parent is nowhere (not in `processing/` and not in `outbox/`), the subtask is moved to `failed/`. `recover_stalled_subtasks` applies the same outbox/ check and skips silently instead of emitting debug noise every cycle.
- **SIGINT isolation:** agent subprocesses are spawned with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` (Windows) or `start_new_session=True` (Unix) so a Ctrl+C in the scheduler terminal does not propagate to agents mid-LLM-call, preventing task orphaning.
- **Startup health checks:** before spawning any agents, the scheduler (a) flushes all `__pycache__` directories under `scripts/` so agents always import fresh bytecode, and (b) test-imports `shared/task_io.py` — if it fails to import, the scheduler logs a FATAL error and aborts without starting any agents.
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
- **Config** centralized in `config.json`; loaded via `scripts/shared/config.py` (`ProjectConfig` class); Ollama timeout is `240s`
- **Dashboard** is a separate Flask process (`dashboard/app.py`); reads directly from the shared filesystem — no DB required; tasks can be submitted from the **Submit Task** tab; results browsable by agent in the **Results** tab
- **Log timestamps** use `datetime.fromtimestamp(time.time(), tz=timezone.utc)` — correct UTC on Windows

## Folder Structure

```
AI Team/
  CLAUDE.md                              ← you are here
  ARCHITECTURE.md                        ← full design doc
  DASHBOARD.md                           ← dashboard usage and API reference
  config.json                            ← centralized config
  RUN_SCHEDULER.bat / RUN_SCHEDULER.sh   ← quick-start scripts
  requirements.txt
  inbox/                   ← drop .task.md files here to submit work
  processing/              ← parent tasks held during validation loop (+ orchestrator.lock)
  validation/              ← completed subtasks awaiting orchestrator approval
  outbox/                  ← approved & completed results
  failed/                  ← QA failure reports + hard-errored tasks
  context/                 ← optional shared context files for tasks
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
      web_search.py       ← DuckDuckGo search wrapper
      logger.py           ← UTC-correct logger
      config.py           ← config.json loader
    agent_orchestrator.py
    agent_coder.py
    agent_research.py
    agent_claude_code.py
    agent_qa.py
    scheduler.py
```

## Running the System

**Terminal 1 — Agents:**
```
RUN_SCHEDULER.bat        (Windows)
RUN_SCHEDULER.sh         (Linux/Mac)
```
Or manually: `python scripts/scheduler.py`

Logs: `logs/scheduler/general.log` and `logs/<agent>/general.log`. Press Ctrl+C to stop.

**Terminal 2 — Dashboard (optional):**
```bash
python dashboard/run_dashboard.py
```
Open `http://localhost:5000`. Runs independently of the scheduler.

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

1. **Parent-child UI** — dashboard currently shows a flat task 