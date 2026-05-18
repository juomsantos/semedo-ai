# Remaining Audit Issues

This document tracks audit findings from the original security & code-quality audit that have **not** yet been addressed. The Critical (Cn), High (Hn), and Major (Mn) items closed so far are listed at the bottom for context.

Last updated: 2026-05-18 (after N1/N2 cleanup).

---

## Nitpicks (low priority, but worth tracking)

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
| N1 | Magic numbers hoisted to module-top constants in `agent_orchestrator.py` | `d206e22` |
| N2 | Late `import re` hoisted to module top in `task_io.py` (+3 sites in `agent_orchestrator.py`) | `d206e22` |
