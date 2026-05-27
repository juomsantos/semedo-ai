# Comprehensive Application Audit — AI Team Multi-Agent System

## Context

Full-stack audit requested covering: backend bugs, code quality, stale code, standards compliance, and a frontend UI/UX review of the dashboard. Three parallel Explore agents covered (1) backend agents/orchestration/shared, (2) dashboard backend + frontend JS/CSS, (3) tests/RAG API/config. Critical findings were verified by direct file reads.

---

## CRITICAL (Fix Immediately)

### C1 — API Key Committed to Git (`config.json:3`)
**Real secret exposed in source:**
```json
"ollama_api_key": "4bdec1afd22743ffa6aa1be921e51d40.GLvze_jHdOTeNhAL4O-h4jKl"
```
- Plaintext in a tracked file; in git history even if removed now
- **Fix:** Rotate/revoke this key. Move to `$OLLAMA_API_KEY` env var. Read in `shared/config.py`. Add `config.json` to `.gitignore` or strip the key field. Purge from git history with `git filter-repo`.

### C2 — XSS in Task Detail Modal (`dashboard.js:942–948`)
Log fields are interpolated directly into `innerHTML` with no escaping:
```js
<span class="log-timestamp">${log.timestamp}</span>
<span class="log-level ${log.level.toLowerCase()}">${log.level}</span>
<span class="log-agent">${log.agent}</span>
<span class="log-message">${log.message}</span>
```
Any agent or task that writes HTML into a log line executes in the browser.
- **Fix:** Wrap all four fields with `escapeHtml()` before interpolation.

---

## HIGH Severity

### H1 — Path Traversal in `_is_safe_output_path` (`validate.py:81–88`)
Current check only tests `".." not in p.parts` and leading slashes. It does not resolve the path against `PROJECT_ROOT`, so crafted relative paths that don't contain literal `..` segments could still escape the `outputs/` directory.
- **Fix:** After the `..` check, resolve `outputs_dir / rel_path` and assert it is under `outputs_dir`, matching the pattern in `safe_read_context()`.

### H2 — RAG API Has No Authentication (`rag_api/main.py`)
`POST /ingest`, `DELETE /documents/{id}`, and `GET /query` are open to any local process with no token/key. Destructive endpoints (ingest, delete) should require a shared secret.
- **Fix:** Add an `X-RAG-Token` header check mirroring the dashboard's `X-Dashboard-Token` approach. Token read from env var or `config.json`.

### H3 — Document ID Hash Collision (`rag_api/vector_store.py:74`)
```python
doc_id = doc.get('id') or f"doc_{abs(hash(content)) % 10_000_000}"
```
Python `hash()` is salted per-process (since 3.3) and modulo 10M means ~1-in-10M collision per doc. Two docs with the same computed ID silently overwrite.
- **Fix:** Replace with `f"doc_{uuid.uuid4().hex[:16]}"`.

### H4 — Race Condition in RAG API Restart (`scheduler.py:110–138`)
`poll()` → stderr read → restart has a window where the process can crash again during the stderr read, potentially triggering rapid unbounded restarts.
- **Fix:** Assign `self._rag_process = None` immediately after `poll()` returns non-None, before the stderr read. Wrap stderr read in its own try/except with a timeout.

---

## MEDIUM Severity

### M1 — Validation Repair Thread Leaks Stale Result (`validate.py:~285`)
After a 300s timeout the function returns `_VALIDATION_PARSE_FAILED`, but the daemon thread continues running and eventually writes into `_repair_result[0]`. Subsequent validation calls may read a stale result from a timed-out prior request.
- **Fix:** Use a `threading.Event` cancellation flag, or timestamp the result and discard any result older than the initiating call.

### M2 — Timestamp String Comparison in QA Chain (`qa_chain.py:~135`)
ISO 8601 timestamps are compared with `<` as strings. Non-zero-padded timestamps (e.g. `"2025-5-3T..."`) silently produce wrong ordering.
- **Fix:** Parse with `datetime.fromisoformat()` in a try/except; fall back to string comparison with a warning log.

### M3 — Empty QA Response Has No FAIL Sentinel (`agent_qa.py:~372–383`)
After 2 empty-response retries the loop exits and falls through to verdict parsing with `response = ""`, producing an ambiguous downstream state.
- **Fix:** After the retry loop, check `if not response.strip(): return default_fail_verdict(task)`.

### M4 — Unbounded Context Injection in Chat (`chat_context.py:59–118`)
All active tasks are dumped into every chat system prompt. With 100+ active tasks this overflows context windows and wastes tokens.
- **Fix:** Cap at 30 active tasks and 10 completed tasks in the snapshot. Add `"... and N more"` truncation note.

### M5 — Session Memory Leak (`chat_session.py`)
Chat sessions are stored in-memory and never expired. Long-running dashboard processes accumulate sessions indefinitely.
- **Fix:** Add a TTL (e.g. 2 hours idle) and prune sessions on each new session creation.

### M6 — Embedding Fallback Hardcodes 4096 Dims (`rag_api/vector_store.py:42–57`)
```python
except Exception:
    return [0.0] * 4096
```
If a different embedding model is configured, dimension mismatch silently corrupts the vector store.
- **Fix:** Read expected dims from config or store in metadata on first successful embed. Log a warning on fallback.

### M7 — Chunk Overlap Semantics Mismatch (`rag_api/ingestion.py:56–58`)
`chunk_overlap` is documented in characters but used to slice a sentence list by count (`current_chunk[-self.chunk_overlap:]`), so actual overlap is unpredictable.
- **Fix:** Either apply overlap as characters consistently, or rename the parameter to `overlap_sentences` and document the unit.

### M8 — Task Dependency Mutation Without Atomicity (`task_io.py:~308`)
`resolve_task_dependencies()` mutates `task["meta"]` in memory then calls `write_result()`. If the write fails, `depends_on` is already removed from the in-memory dict but the on-disk file is unchanged — leaving a task in permanent limbo.
- **Fix:** Write to a `.tmp` file first, then atomic rename; or catch `write_result` exceptions and restore `depends_on` on failure.

---

## FRONTEND / UI/UX Findings

### F1 — Duplicate `escapeHtml` Definition (`dashboard.js:1366` and `1418`)
Two byte-identical functions defined 52 lines apart. One is dead code.
- **Fix:** Delete lines 1418–1422 (the second definition in the Chat section).

### F2 — Duplicate "Chat Functions" Section Header (`dashboard.js:1372–1378`)
Two identical `// ===... Chat Functions ...===` banners back-to-back. Dead comment noise.
- **Fix:** Delete one of the two repeated header blocks.

### F3 — No 401 Detection / Server-Restart Notification
When the dashboard server restarts and generates a new token, open browser tabs silently fail all state-changing requests with 401. Users have no indication.
- **Fix:** In `withAuth()`, intercept 401 responses and display a banner: "Dashboard restarted — please refresh the page."

### F4 — CDN Scripts Loaded Without SRI Hashes (`index.html`)
`marked.js`, `DOMPurify`, and `highlight.js` are loaded from cdnjs without `integrity=` attributes. A compromised CDN serves arbitrary JS to the dashboard.
- **Fix:** Add `integrity="sha384-..."` and `crossorigin="anonymous"` to all three `<script>` tags. Hashes are available on cdnjs.

### F5 — No Content-Security-Policy (`index.html`)
No CSP means any future XSS is fully exploitable with no browser-level mitigation.
- **Fix:** Add a `<meta http-equiv="Content-Security-Policy">` tag restricting scripts to `'self'` and the specific CDN origins.

### F6 — Task Detail Modal Error Not Surfaced (`dashboard.js:958–960`)
The `catch` block only `console.error`s; the modal body stays on "Loading..." forever on API error or 404.
- **Fix:** In the `catch`, set `body.innerHTML = '<p class="error">Failed to load task details.</p>'`.

### F7 — `clearChatHistory` Silent No-Op (`dashboard.js:~1719–1737`)
If called with no active session, the function returns with no user-visible feedback. User clicks "Clear" and nothing happens.
- **Fix:** Show a toast or status message "No active chat session to clear."

### F8 — Polling Never Paused (`dashboard.js:147–163`)
`startPolling()` runs every 1.5 s unconditionally. No pause on `visibilitychange` or before page unload. Wasted requests when the tab is backgrounded.
- **Fix:** Add `document.addEventListener('visibilitychange', ...)` to stop/start polling based on tab visibility.

### F9 — Log Column Grid Not Responsive (`dashboard.css`)
`.log-line { grid-template-columns: 148px 58px 110px 1fr }` overflows on narrow screens.
- **Fix:** Add a `@media (max-width: 768px)` rule that collapses to a single-column or two-column layout.

### F10 — Missing `<details>`/`<summary>` in DOMPurify Allowlist (`dashboard.js:1408–1415`)
Thinking-mode collapsible blocks need `<details>/<summary>` tags but they're absent from `ALLOWED_TAGS`, so they're silently stripped on render.
- **Fix:** Add `'details'` and `'summary'` to the `ALLOWED_TAGS` array.

---

## LOW / Code Quality

### L1 — Unused Import in `agent_qa.py:38`
`web_fetch` imported but never referenced in the file.
- **Fix:** Remove `web_fetch` from the import line.

### L2 — Dead Code in `agent_qa.extract_code` (`agent_qa.py:130–133`)
A fallback `re.search` after a loop that already exhausts all code-block patterns — unreachable.
- **Fix:** Delete lines 130–133.

### L3 — Magic Numbers Scattered Across Agents
`MAX_SEARCH_TURNS`, `MAX_FETCH_TURNS`, `STALE_THRESHOLD_SECONDS`, `MAX_RESULT_CHARS` are redefined per-file with slightly different values. Tuning requires editing multiple files.
- **Fix:** Consolidate into `scripts/shared/constants.py` and import from there.

### L4 — Rerank Status Missing from RAG Query Response (`rag_api/main.py`)
When reranking silently fails, the response looks identical to a successfully-ranked response. Callers can't detect degraded quality.
- **Fix:** Add a `"reranked": bool` field to `QueryResponse`.

### L5 — No Rate Limiting on RAG API
`/ingest` and `/query` with reranking trigger multiple embedding calls with no rate guard.
- **Fix:** Add `slowapi` middleware with a generous limit (e.g. 30 req/min per IP).

### L6 — `_get_agent_stats` Hardcodes Agent Names (`task_monitor.py:~262–299`)
New agents added to the system won't appear in stats without editing this function.
- **Fix:** Scan the `agents/` directory to discover agent names dynamically.

### L7 — Firefox / Responsive Polish (CSS)
- `backdrop-filter: blur(3px)` has no Firefox fallback
- `::-webkit-scrollbar` rules don't apply to Firefox/Edge
- Chat bubble `max-width: 72%` is too narrow on mobile

These are visual inconsistencies, not bugs. Fix with `@supports` guards and responsive breakpoints.

---

## What Is in Good Shape (No Action Needed)

- **Test suite**: 267 tests, all passing, comprehensive coverage, no flaky patterns, meaningful assertions.  
- **Error handling patterns**: pre-flight checks, OllamaError handling, outer loop guards — consistently applied across all agents.
- **Task state machine**: inbox → processing → validation → outbox lifecycle is well-designed with four recovery paths at startup.
- **`mark_processing()` regex**: correctly avoids python-frontmatter round-trips that drop fields.
- **`safe_read_context()`**: proper PROJECT_ROOT bounds check — this pattern should be extended to `_is_safe_output_path`.
- **Dashboard auth (state-changing endpoints)**: `X-Dashboard-Token` correctly guards all POST/DELETE.
- **`markdownToHtml`**: DOMPurify is used correctly on all LLM/markdown output.
- **Dependencies**: all pinned to exact versions, no known CVEs, transitive lock file present.
- **SIGINT isolation**: `CREATE_NEW_PROCESS_GROUP` correctly prevents Ctrl+C propagation to agent subprocesses.

---

## Prioritized Fix Order

| Priority | Items | Effort |
|---|---|---|
| 1 — Do today | C1 (rotate key), C2 (XSS escapeHtml), H1 (path traversal) | Small |
| 2 — This week | H2 (RAG auth), H3 (UUID doc IDs), H4 (restart race), F1/F2 (duplicate code) | Small–Medium |
| 3 — Next sprint | M1–M8, F3–F10, L1–L2 | Medium |
| 4 — Backlog | L3–L7 | Low |

---

## Verification Plan

After fixes are applied:
1. **C1**: `git log --all --oneline -- config.json` confirms key removed from history; `$OLLAMA_API_KEY` is read at startup.
2. **C2**: Open task detail modal for a task whose log contains `<img src=x onerror=alert(1)>` — must render as escaped text, no alert.
3. **H1**: Submit a coder task that outputs a file named `../../evil.py` — must be rejected at extraction, not written.
4. **H2**: `curl -X POST http://localhost:8000/ingest -d '...'` without token — must return 401.
5. **H3**: Ingest two docs with same content — must get distinct IDs in ChromaDB.
6. **F6**: Kill the scheduler mid-load, open task detail — modal must show error message.
7. **F10**: Send a chat message that produces thinking content — `<details>/<summary>` must render in the bubble.
8. Run full test suite (`pytest`) — must stay green throughout.
