# Remaining Audit Issues

This document tracks audit findings from the original security & code-quality audit that have **not** yet been addressed. The Critical (Cn), High (Hn), and Major (Mn) items closed so far are listed at the bottom for context.

Last updated: 2026-05-18 (after N7 cleanup).

---

## Nitpicks (low priority, but worth tracking)

### N6 — Per-call `validation/` folder scans

- The orchestrator and dashboard both scan `validation/*.task.md` on every cycle / API call. Fine at current scale (dozens of files); will get hot if the project ever sees hundreds of in-flight subtasks.
- **Fix:** Cache the listing for the duration of one orchestrator cycle; invalidate on file-watcher events. Defer until performance actually matters.
- **Effort:** ~1 hr (and only worth doing if a hot-spot shows up in profiling).

### N7 (partial — status codes) — Detailed error redaction still open

- The status-code half of N7 was closed (see closed table). The other half — replacing `str(e)` in error bodies with a generic message + a server-side UUID for log correlation — was intentionally deferred. Dashboard responses still leak internal error text to the browser. Acceptable while the dashboard stays loopback-only; revisit if it's ever exposed beyond `127.0.0.1`.
- **Fix:** In `_json_error_envelope` (`dashboard/app.py`), replace `str(e)` with `f"Internal error ({uid})"` for the 500 branch and log `uid → traceback` server-side. Keep the 400/404/503 branches as-is so client-input errors still tell the user what went wrong.
- **Effort:** ~30 min.

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
| N1 | Magic numbers hoisted to module-top constants in `agent_orchestrator.py` | `d206e22` |
| N2 | Late `import re` hoisted to module top in `task_io.py` (+3 sites in `agent_orchestrator.py`) | `d206e22` |
| N3 | Narrowed 4 JSON-parse `except Exception` → `ValueError` in orchestrator. Audit confirmed remaining broad catches are intentional log-and-continue per CLAUDE.md error-handling pattern (3). | `1adb4f6` |
| N4 | Obsolete — closed by refactor. The dashboard no longer has an approval-detail modal or `approvalsCache`; approvals render inline with all needed metadata. No new endpoint needed. | n/a |
| N5 | `DEFAULT_CHAT_MODEL` / `DEFAULT_CHAT_TIMEOUT_S` / `DEFAULT_CHAT_MAX_TOOL_TURNS` / `DEFAULT_RAG_BASE_URL` hoisted to module-top in `dashboard/app.py`; used in both config-loaded and outer-fallback paths. | `1adb4f6` |
| N7 (status codes) | `_json_error_envelope` decorator in `dashboard/app.py`: ValueError → 400, FileNotFoundError → 404, ConnectionError → 503, fallback → 500. Applied to 17 endpoints (skipping `clear_cache` and `rag_status` which have intentionally different response shapes). Error-message redaction still pending — see N7 partial entry. | (pending) |
