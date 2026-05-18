# Remaining Audit Issues

This document tracks audit findings from the original security & code-quality audit that have **not** yet been addressed. The Critical (Cn), High (Hn), and Major (Mn) items closed so far are listed at the bottom for context.

Last updated: 2026-05-18 (after the M6 PR).

---

## Critical

### C1 — Plaintext Ollama API key in `config.json`

- **File:** [config.json:3](config.json:3)
- **Current state:** The key is still present in plaintext on disk:
  ```
  "ollama_api_key": "4bdec1afd22743ffa6aa1be921e51d40.GLvze_jHdOTeNhAL4O-h4jKl"
  ```
- **Risk:** `config.json` is gitignored, so the secret isn't in git history, but it is still readable by any process running as the local user, by backup tooling, and by any extension or telemetry agent that snapshots the home directory. `ollama_client.py` exports it into `os.environ["OLLAMA_API_KEY"]` at import time, so it also leaks into the env of every spawned agent subprocess (visible in `Get-Process | Select-Object StartInfo` on Windows and `/proc/<pid>/environ` on Linux).
- **Fix:** Move the key out of `config.json` into either (a) `$OLLAMA_API_KEY` environment variable (set via a `.env` file or shell rc, with `python-dotenv` for dev), or (b) the OS keyring (`keyring` package). Replace the config field with `"ollama_api_key_env": "OLLAMA_API_KEY"` and have `config.py` read the named env var. Add a one-line `config.example.json` that documents the new shape. Rotate the existing key after the change lands.
- **Effort:** ~30 min.

---

## Major

### M1 — `agent_orchestrator.py` is 1,483 lines in a single file

- **File:** [scripts/agent_orchestrator.py](scripts/agent_orchestrator.py) (currently 1,483 LOC)
- **Risk:** Validation loop, decomposition, recovery, dependency wiring, and retry-coder logic all live in one module. Bug surface area is wide; every code reader has to load the full file into their head. The pure helpers (`_find_qa_for_coder_subtask`, `_extract_qa_verdict`, etc.) are already covered by tests, but the LLM-driven paths are not — splitting would let us test the dispatch logic without mocking out the entire orchestrator.
- **Fix:** Carve into a package, behaviour-preserving:
  - `scripts/orchestration/decompose.py` — decomposition LLM call, `redecompose_after_research`.
  - `scripts/orchestration/validate.py` — validation LLM call, QA gate, decision handling.
  - `scripts/orchestration/recovery.py` — `recover_orphaned_tasks`, `recover_processing_subtasks`, `recover_stalled_subtasks`, `recover_orphaned_validation_subtasks`.
  - `scripts/orchestration/dispatch.py` — subtask file creation + worker routing.
  - `scripts/agent_orchestrator.py` — thin `main()` wiring loop (≤150 lines).
- **Effort:** ~2 hrs. No behaviour change; full pytest suite must remain green.

### M7 — `agent_claude_code.py` token logging is approximate

- **File:** [scripts/agent_claude_code.py:115](scripts/agent_claude_code.py:115)
- **Current state:** The agent now calls `log_tokens()` with a word-count approximation (`len(response.split())`), which is better than the original "no logging at all" state but still understates real spend (Claude tokens ≠ words; a typical English token is ~0.75 words).
- **Risk:** Dashboard token totals for claude-code are misleading. Quietly accepted today; should be documented in the UI (Agent Stats tab) as "approximate, CLI does not report token counts" so João isn't surprised when his Anthropic bill diverges from the dashboard.
- **Fix:** Either (a) call the Anthropic API directly via the `anthropic` SDK and get real token counts, or (b) keep the approximation and add a footnote in the dashboard. (b) is the cheap fix; (a) is the right fix but adds a new dependency and an API key to manage (which collides with C1).
- **Effort:** (b) ~15 min, (a) ~2 hrs.

---

## Nitpicks (low priority, but worth tracking)

### N1 — Magic numbers buried inline

- [agent_orchestrator.py:519](scripts/agent_orchestrator.py:519) `MAX_RESULT_CHARS = 256000`
- [agent_orchestrator.py:671](scripts/agent_orchestrator.py:671) `STALE_THRESHOLD_SECONDS = 720`
- [agent_orchestrator.py:730](scripts/agent_orchestrator.py:730) `MAX_STALL_RETRIES`
- **Fix:** Hoist to module-top constants with comments, or to `config.json`. They are documented in CLAUDE.md but a config-driven knob is easier for João to tune without code edits.
- **Effort:** ~15 min.

### N2 — Late `import re` inside hot path

- [scripts/shared/task_io.py:111](scripts/shared/task_io.py:111), [task_io.py:127](scripts/shared/task_io.py:127): `import re as _re` is still inside `mark_processing` and `mark_awaiting_validation`.
- **Fix:** Move `import re` to module top. `import` is idempotent and cheap, but module-top is conventional and makes static analysis easier.
- **Effort:** ~2 min.

### N3 — Over-broad `try` blocks

- Several `try: ... except Exception:` blocks remain in `agent_orchestrator.py` despite the C4 pass. C4 narrowed the ones that masked real bugs; the survivors are largely "log and continue" guards in the outer task loop. Worth a follow-up audit to confirm each surviving broad-catch is intentional.
- **Effort:** ~30 min audit.

### N4 — Dashboard `approvalsCache` is the only source of truth

- [dashboard/static/dashboard.js:60](dashboard/static/dashboard.js:60) — there is still no `GET /api/pending-approvals/<id>` endpoint; the modal reads from the in-memory cache populated by the polling loop. If a user opens an approval detail after a server restart (cache empty) the modal renders `undefined` until the first poll completes.
- **Fix:** Add a small `GET /api/pending-approvals/<id>` endpoint that re-reads the task file from `agents/claude-code/pending/`. Use it as the source of truth in `showApprovalDetail()`; keep the cache as an optimistic prefill only.
- **Effort:** ~30 min.

### N5 — Hardcoded dashboard fallback values

- [dashboard/app.py:112](dashboard/app.py:112), [app.py:116](dashboard/app.py:116): `"qwen3.5:9b"` appears twice as a fallback model. Same pattern likely repeats for chat timeouts and tool-turn limits.
- **Fix:** Define module-level constants (`DEFAULT_CHAT_MODEL`, `DEFAULT_CHAT_TIMEOUT_S`, etc.) and use them in both the config-loaded and fallback paths.
- **Effort:** ~15 min.

### N6 — Per-call `validation/` folder scans

- The orchestrator and dashboard both scan `validation/*.task.md` on every cycle / API call. Fine at current scale (dozens of files); will get hot if the project ever sees hundreds of in-flight subtasks.
- **Fix:** Cache the listing for the duration of one orchestrator cycle; invalidate on file-watcher events. Defer until performance actually matters.
- **Effort:** ~1 hr (and only worth doing if a hot-spot shows up in profiling).

### N7 — Dashboard endpoints collapse all errors to 500

- [dashboard/app.py](dashboard/app.py) — typical pattern: `except Exception as e: return jsonify({"error": str(e)}), 500`. Hides distinction between client errors (bad input → 400) and server errors (genuine bug → 500), and leaks internal error messages back to the browser.
- **Fix:** Narrow the catches: `ValueError` → 400, `FileNotFoundError` → 404, everything else → 500 with a generic message + a UUID logged server-side that the user can quote in a bug report.
- **Effort:** ~45 min.

### N8 — `requirements.txt` uses `>=` only

- `requirements.txt` and `rag_api/requirements.txt` both pin with `>=`. A `pip install` on a fresh machine in 6 months will pull whatever's current on PyPI — that's the route through which silent supply-chain compromises arrive, and the route through which "works on my machine" bugs arrive too.
- **Fix:** Pin direct dependencies to `==`. Generate a `requirements.lock` via `pip freeze` for the working set, check it in, and document the workflow ("upgrade with `pip-compile`, then commit the new lock").
- **Effort:** ~20 min.

### N9 — RAG API `Settings` uses raw `os.getenv`

- [rag_api/config.py](rag_api/config.py) — explicitly documented as "**not** `pydantic_settings.BaseSettings`" because pydantic-settings isn't installed. That's fine, but the hand-rolled approach skips type coercion (every config value is a string until you `int()` it) and silently accepts typos in env var names.
- **Fix:** Either add `pydantic-settings` to `rag_api/requirements.txt` and use `BaseSettings`, or stay hand-rolled but add a `_REQUIRED_KEYS` sanity check at startup so a typo crashes fast instead of producing a confusing runtime error 20 minutes in.
- **Effort:** ~30 min.

### N10 — No resource limits on agent subprocesses

- [scripts/scheduler.py:70-83](scripts/scheduler.py:70) — agents are spawned with `subprocess.Popen` and no memory/CPU caps. A runaway LLM tool loop, a leaking ChromaDB client, or a coder agent that fork-bombs each other could wedge the host.
- **Fix:** On POSIX, wrap the agent entrypoint in `resource.setrlimit(RLIMIT_AS, ...)`. On Windows, use Job Objects via `psutil` or `pywin32`. Document in CLAUDE.md as a known limitation if the Windows path is too painful.
- **Effort:** ~1 hr (POSIX), ~3 hrs (cross-platform).

---

## Verification checklist

When closing any of the above, the following must remain green:

```
pytest tests/ -q       # currently 219/219 (1 environment-dependent flake in test_rag_injection.py)
```

For security-relevant changes (C1, N7), add a regression test under `tests/`. For the orchestrator split (M1), the existing `tests/test_orchestrator_helpers.py` is the safety net — every helper that moves must keep its existing test passing without changes to the test file.

---

## Already closed (for context)

| ID | Title | Commit |
| --- | --- | --- |
| C2 | DOM-XSS via inline `onclick` in Approvals panel | `f6178d6` |
| C3 | Path-traversal via `context_files` | `f6178d6` |
| C4 | Bare `except Exception` masking real bugs | `f6178d6` |
| C5 | Hardcoded LAN-IP fallback in `ollama_client.py` | `f6178d6` |
| H1 | RAG API bound to `0.0.0.0` without auth | `f6178d6` |
| H2 | Dashboard CORS + shared-secret token guard | `f6178d6` + `c7ec380` |
| H3 | RAG `/ingest` had no payload-size cap | `f31748f` |
| H4 | No test suite | `fb3cc0c` (initial pytest bootstrap) |
| M2 | Duplicated RAG pre-prompt block → `shared/rag_injection.py` | `6c8c181` |
| M3 | Inconsistent agent error-handling philosophy | `19a904c` |
| M4 | `validation_context` not propagated to QA | `dba7172` |
| M5 | Hand-rolled YAML parser in dashboard → `yaml.safe_load` | `6570b67` |
| M6 | Duplicated agent boilerplate → `shared/agent_boilerplate.py` | `8e13b45` |
