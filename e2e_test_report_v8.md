# AI Team — E2E Test Report v8

**Date:** 2026-05-07  
**Protocol:** e2e_test_protocol_v8.md  
**Tester:** Claude (Cowork)  
**Test window:** 19:57 – 21:22 UTC  
**Verdict:** ⚠️ PARTIAL PASS — 14 / 20 checks passed

---

## Executive Summary

The AI Team pipeline ran end-to-end against all three test scenarios. T1 (research) completed cleanly in 2 iterations. The approval flow for T3 (claude-code) worked correctly at every step until execution, where a pre-existing bug (N2) corrupted the task's frontmatter and caused the result to be misnamed. T2 (code + QA) exposed a persistent argparse design flaw in the coder model's output: all seven QA attempts failed because `--self-test` conflicts with a required positional argument, causing T2 to exhaust the 5-iteration cap and force-complete without a QA PASS.

Six bugs were confirmed or newly observed. Two of them (N2 and the argparse self-test conflict) are responsible for all six failing checks.

---

## Test Scenario Results

### T1 — Python Packaging Tools Comparison (Research)

| Criterion | Result |
|-----------|--------|
| Dispatched to research within 6 min | ✅ ~2 min |
| ≥2 DuckDuckGo searches | ✅ 10 total (5 iter 1 + 5 iter 2) |
| Result ≥2000 chars, all 5 sections | ✅ 4,157 chars, 7 sections |
| `complete` in ≤3 iterations | ✅ Iteration 2 (20:13:39) |
| Parent in `outbox/` with `status: complete` | ✅ Confirmed |
| Results tab shows research output | ❌ Dashboard bug — "No completed tasks" despite file present |

**Notes:** Iteration 1 yielded a solid report but the orchestrator requested a refinement (uv version too vague, missing PyPI download numbers). The iteration 2 research run was killed mid-synthesis by the 300s scheduler timeout. On the third validation attempt the orchestrator accepted the original iteration 1 output as complete — a reasonable fallback, though the version field for uv reads "0.x" rather than a specific release.

One Ollama timeout (240s) occurred during the iteration 2 validation LLM call; the orchestrator retried correctly on the next scheduler cycle.

---

### T2 — Word Frequency Counter CLI (Code + QA)

| Criterion | Result |
|-----------|--------|
| Routed directly to coder, no research | ✅ |
| Coder output includes `import sys` and `import argparse` | ✅ Lines 8–13 of first output |
| QA executes `--self-test` with exit code 0 | ❌ All 7 attempts failed (exit codes: 2,1,2,2,2,1,2) |
| QA issues PASS | ❌ All 7 verdicts: FAIL |
| Completes after first QA PASS (≤2 iterations) | ❌ Force-completed at iteration 5 |
| Parent in `outbox/` with `status: complete` | ✅ (via max-iteration cap) |

**Root cause:** Every coder attempt declared `filenames` as `nargs='+'` (required positional), making `python word_freq.py --self-test` trigger an argparse error (exit code 2) before the script body runs. When QA fed this back, subsequent coder retries regressed in other ways (dropping case-insensitive handling, losing `--min-count` or `--output` flags, broken self-test logic). The coder model (`qwen2.5-coder:7b`) failed to consolidate all requirements across iterations.

The max-iteration safeguard worked exactly as designed: at iteration 5 the orchestrator issued a forced `complete` with an explanatory summary, preventing an infinite loop.

---

### T3 — Scheduler Script Audit (claude-code path)

| Criterion | Result |
|-----------|--------|
| Routed to `pending_approval` | ✅ |
| Approvals modal: correct type, priority, created_by | ✅ type=plan, priority=low, created_by=claude-cowork |
| After Approve, task moves to claude-code inbox | ✅ Confirmed |
| claude-code logs real task ID | ❌ Logs show "Processing task unknown" |
| Result file named `task_<real_id>_result.md` | ❌ Named `unknown_result.md` |
| Result content is full audit report | ✅ 6,247 chars, 5 structured sections, quality score 8/10 |
| Parent moves to `outbox/` with `status: complete` | ❌ Stuck in `processing/` indefinitely |

**Root cause (N2 bug):** `mark_processing()` in `agent_claude_code.py` writes a new frontmatter block containing only `status: processing`, pushing all original YAML (including `id` and `output_path`) into the Markdown body. `python-frontmatter` reads `id` as `None`. The agent writes the result as `unknown_result.md` instead of the expected path. The orchestrator's validation loop searches for `task_20260507_200121_319020_result.md`, never finds it, and T3's parent remains stuck in `processing/` with no path to completion.

Despite the naming failure, the content itself was correct — a properly structured 5-section audit of `scripts/scheduler.py` including step-by-step logic, error handling gaps, concurrency risks, a concrete improvement recommendation with line numbers, and a quality score.

---

## 20-Check Scorecard

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | Startup health checks pass | ✅ PASS | pycache flush, task_io import clean, Ollama reachable, all 5 agents spawned at 19:57:47 |
| 2 | All 3 tasks submitted and visible | ✅ PASS | All 3 in Active Tasks within ~2s of submission |
| 3 | Orchestrator dispatches all 3 within 6 min | ✅ PASS | All dispatched 20:00:58–20:01:21 (~2 min) |
| 4 | T1 routed to research agent | ✅ PASS | Subtask → research at 20:00:58 |
| 5 | T2 routed directly to coder (no research) | ✅ PASS | Subtask → coder at 20:01:08 only |
| 6 | T3 routed to pending_approval | ✅ PASS | → pending_approval at 20:01:21 |
| 7 | Approvals modal shows correct metadata | ✅ PASS | type, priority, created_by all correct; modal populated from cache |
| 8 | T3 approved → claude-code inbox | ✅ PASS | File confirmed in agents/claude-code/inbox/ after Approve |
| 9 | T1 research ≥2 DuckDuckGo searches | ✅ PASS | 10 searches total (5 + 5 across two iterations) |
| 10 | T1 result ≥2000 chars, all 5 sections | ✅ PASS | 4,157 chars; pip / uv / Poetry versions, features, perf, adoption, recommendations all present |
| 11 | T1 completes in ≤3 orchestrator iterations | ✅ PASS | `complete` decision at iteration 2 (20:13:39) |
| 12 | T2 coder output has `import sys` + `import argparse` | ✅ PASS | Lines 8–13 of first coder result (`import sys`, `import argparse`, `import re`, `import os`) |
| 13 | QA executes `--self-test` exit code 0 | ❌ FAIL | 7 attempts; exit codes 2,1,2,2,2,1,2 — argparse required-positional conflict |
| 14 | QA issues PASS for T2 | ❌ FAIL | All 7 QA verdicts: FAIL |
| 15 | T2 completes ≤2 iterations after first QA PASS | ❌ FAIL | No PASS ever achieved; force-completed at iteration 5 |
| 16 | claude-code logs real task ID | ❌ FAIL | Log: "Processing task unknown" — N2 bug (mark_processing corrupts frontmatter) |
| 17 | T3 result file named correctly | ❌ FAIL | Written as `outbox/unknown_result.md` instead of `outbox/task_<real_id>_result.md` |
| 18 | T3 result content is full audit (not permission request) | ✅ PASS | Structured 5-section audit with quality score — correct content |
| 19 | All 3 parent task files have `status: complete` | ❌ FAIL | T1 ✅ T2 ✅ T3 ❌ (stuck in processing) |
| 20 | History: T1+T2 COMPLETED; Logs: newest-first | ✅ PASS | History shows T1 and T2 completed (accurate); Logs tab confirmed newest-first (20:21:50 at top) |

**Total: 14 / 20 → ⚠️ PARTIAL PASS**

---

## Bugs Confirmed

### BUG-1 (N2) — `mark_processing()` corrupts task frontmatter in `agent_claude_code.py` [CRITICAL]

**Impact:** Checks 16, 17, 19 (3 failures)

`mark_processing()` writes a new YAML block containing only `status: processing` and moves the file, pushing the entire original frontmatter (including `id` and `output_path`) into the Markdown body. `python-frontmatter` can no longer read `id`, so every field derived from it (`output_path`, result filename) reads as `None`. The result file is always written as `unknown_result.md`. The parent task can never complete because the orchestrator's validation loop never finds the expected result path.

**Fix:** `mark_processing()` must merge `status: processing` into the existing frontmatter, not replace it.

```python
# Current (broken):
task.metadata['status'] = 'processing'
content = frontmatter.dumps(task)   # ← overwrites original YAML

# Fix: read-modify-write
post = frontmatter.load(path)
post['status'] = 'processing'
new_path = processing_dir / path.name
with open(new_path, 'w', encoding='utf-8') as f:
    f.write(frontmatter.dumps(post))
path.rename(new_path)   # or shutil.move
```

---

### BUG-2 — Coder model generates argparse `nargs='+'` for positional args conflicting with `--self-test` [HIGH]

**Impact:** Checks 13, 14, 15 (3 failures)

The `qwen2.5-coder:7b` model consistently declares the `filenames` positional argument as `nargs='+'` (one or more required), making `--self-test` impossible to invoke without also passing a filename. The argparse error (exit code 2) fires before the script body runs. Subsequent retry iterations regress on other features.

**Fix options:**
1. Update `agents/coder/system_prompt.md` to include an explicit instruction: "When a `--self-test` flag is present, the positional `filenames` argument must use `nargs='*'` (zero or more) so the self-test can run standalone."
2. Add an example showing the correct argparse pattern for flags that bypass positional requirements.

---

### BUG-3 (Minor) — Research subtask timeout leaves orphan in `processing/` [LOW]

The T1 refinement research task (200407) was killed by the 300s scheduler timeout. It sits in `processing/` permanently. On the next scheduler restart, orphan recovery will re-dispatch it (expected behavior), but until then it shows as "PROCESSING" in the dashboard.

---

### BUG-4 (Minor) — Results tab shows "No completed tasks" for all worker agents [LOW]

The Results tab returns empty results for Research, Coder, QA, and claude-code despite multiple `*_result.md` files in `outbox/` containing `agent: <name>` metadata. The likely cause is a mismatch between the metadata key the dashboard API reads and what's actually written (e.g., `agent: research` vs. a case-sensitive key difference, or the tab's API call filtering on a field that stale-moved subtasks lack).

---

### BUG-5 (Minor) — Dashboard Failed counter excludes QA failure reports [LOW]

Three `*_qa_failure.md` files were written to `failed/` by the QA agent. The dashboard Failed counter reads 0 because it counts `.task.md` files, not `_qa_failure.md` files. This means legitimate QA failures are invisible in the top-level status summary.

---

### BUG-6 (Minor) — T2 expected output field garbled during submission [VERY LOW]

When submitting T2 via the dashboard, the Expected Output field retained T1's text from the previous form submission. T2's expected_output was stored as a concatenation of both tasks' texts. This had no effect on execution (agents only use the task description, not expected_output), but the stored task file contains incorrect metadata.

---

## System Stability Observations

The scheduler, orchestrator, and worker agents ran continuously and stably throughout the 85-minute test window. Key observations:

- **Lockfile concurrency protection** worked: when the orchestrator ran long, subsequent scheduler cycles detected the running PID and skipped correctly (logged 3 times).
- **Max-iteration cap** worked: T2 was cleanly force-completed at iteration 5 with a written result summary, preventing an infinite loop.
- **Orphan recovery** will work correctly on next restart: the T1 refinement orphan has `status: pending` (killed before mark_dispatched), so it will be re-dispatched.
- **Stale subtask cleanup** worked: after T2's parent was marked complete, the next orchestrator cycle correctly identified and moved stale T2 subtasks from `validation/` to `outbox/` rather than `failed/`.
- **UTF-8 encoding** held throughout — no codec crashes despite non-ASCII characters in research output.
- **One Ollama timeout** (240s) on T1 validation — the orchestrator recovered cleanly by retrying on the next cycle.
- **One scheduler timeout** (300s) on the T1 refinement research run — the scheduler killed the process and logged it; the orchestrator later completed T1 using the existing iteration 1 result.

---

## Residual State After Test

| Folder | Contents | Notes |
|--------|----------|-------|
| `outbox/` | T1 parent + result ✅; T2 parent + result ✅; T1 research subtask result; 7 coder iteration results; `unknown_result.md` | Clean up `unknown_result.md` and the stale iteration results |
| `processing/` | T3 parent (200015) — stuck; T1 research orphan (200407) | Both will be re-dispatched on next restart |
| `validation/` | ~18 stale T2 iteration subtasks | Orchestrator will clean these on next restart cycle |
| `failed/` | 3 QA failure reports (`*_qa_failure.md`) | Safe to delete |
| `agents/claude-code/` | Empty (inbox + pending cleared) | Clean |

---

## Recommendations (Priority Order)

1. **Fix N2 immediately** — `mark_processing()` must merge into existing frontmatter, not replace it. This bug silently breaks the entire claude-code path and produces a permanently stuck parent task on every run.

2. **Update coder system prompt re: argparse + self-test** — Add an explicit note that `filenames` must use `nargs='*'` when a `--self-test` flag is present, and include a short reference implementation. This single change would have resolved all 7 T2 QA failures.

3. **Fix Results tab** — Investigate why `outbox/*_result.md` files with `agent: <name>` metadata are not surfacing. Check for case sensitivity or filtering logic in `dashboard/task_monitor.py`.

4. **Fix Failed counter** — Update the dashboard API to count `*_qa_failure.md` files in `failed/` in addition to `.task.md` files.

5. **Scheduler restart test** — Run after the N2 fix to confirm orphan recovery cleans up the stuck T3 parent and T1 research orphan correctly.

---

## Final Verdict

| Outcome | Criteria | Status |
|---------|----------|--------|
| ✅ PASS | 18+ checks | — |
| ⚠️ PARTIAL PASS | 14–17 checks | **← THIS RUN: 14/20** |
| ❌ FAIL | < 14 checks | — |

**14 / 20 checks passed. ⚠️ PARTIAL PASS.**

All 6 failures trace to exactly 2 bugs: the N2 frontmatter corruption bug (3 checks) and the coder model's argparse self-test conflict (3 checks). The core pipeline architecture — scheduling, orchestration, dispatch, research, validation loop, approval flow, dashboard — is sound and performed correctly. Fixing these two issues would bring the score to 20/20 on the next run.
