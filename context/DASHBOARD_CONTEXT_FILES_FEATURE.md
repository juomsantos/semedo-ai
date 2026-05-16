# Feature Requirements: Context Files Picker on Dashboard Submit Form

## Overview

Add a searchable multi-select picker to the dashboard's **Submit Task** tab that lets users attach completed task results as `context_files` when submitting a new task. The picker fetches completed parent tasks from a new API endpoint, lets the user filter by keyword, select one or more results, and displays the selections as removable chips. Selected output paths are included in the existing `POST /api/tasks/submit` payload.

No new dependencies are required. All changes are confined to four existing files.

---

## Files to Modify

| File | Change type |
|---|---|
| `dashboard/app.py` | Add 1 new endpoint; extend 1 existing endpoint |
| `dashboard/task_monitor.py` | Add 1 new method |
| `dashboard/templates/index.html` | Add context-files UI block inside the submit form |
| `dashboard/static/dashboard.js` | Add picker logic; extend `submitTask()` |
| `dashboard/static/dashboard.css` | Add styles for picker and chips |

---

## 1. `dashboard/task_monitor.py`

### New method: `get_completed_parent_tasks()`

Add this method to the `TaskMonitor` class. It returns a list of completed parent tasks from `outbox/` that have a corresponding `_result.md` file — these are the only ones that make sense as context files.

```python
def get_completed_parent_tasks(self, limit: int = 100) -> list:
    """Return completed parent tasks that have a result file, newest first.

    Each entry is a dict with:
        task_id        - the task ID string
        type           - task type (code / research / etc.)
        created_at     - ISO timestamp string
        description_preview - first 120 chars of the task body
        output_path    - Windows path to the _result.md file (from frontmatter)

    Only parent tasks are included (no parent_task_id in frontmatter).
    Only tasks whose output_path points to an existing _result.md are included.
    """
    results = []
    if not self.outbox.exists():
        return results

    task_files = sorted(self.outbox.glob("*.task.md"), reverse=True)
    for task_file in task_files:
        if len(results) >= limit:
            break
        try:
            task = self._parse_task_file(task_file, "completed", "outbox")
            if task is None:
                continue
            # Skip subtasks
            if task.get("parent_task_id"):
                continue
            output_path = task.get("output_path", "")
            if not output_path:
                continue
            # Verify the result file actually exists
            result_file = Path(output_path)
            if not result_file.exists():
                continue
            results.append({
                "task_id": task["id"],
                "type": task.get("type", "unknown"),
                "created_at": task.get("created_at", ""),
                "description_preview": task.get("body_preview", "")[:120],
                "output_path": output_path,
            })
        except Exception:
            continue

    return results
```

**Important:** `Path(output_path)` works because `task_monitor.py` runs on Windows where the `output_path` strings like `C:\Users\...\outbox\..._result.md` resolve correctly. Do not alter the path — pass it through verbatim to the API response and ultimately into `context_files`.

---

## 2. `dashboard/app.py`

### 2a. New endpoint: `GET /api/tasks/completed`

Add this route after the existing `/api/tasks/submit` route (around line 213):

```python
@app.route("/api/tasks/completed", methods=["GET"])
def get_completed_tasks():
    """Return completed parent tasks available as context files."""
    try:
        tasks = monitor.get_completed_parent_tasks(limit=100)
        return jsonify(tasks), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

Also add the endpoint to the module docstring at the top of `app.py`:

```
  GET /api/tasks/completed - Completed parent tasks for context file selection
```

### 2b. Extend `submit_task()` to accept `context_files`

In the existing `submit_task()` function (around line 198), the `create_task_file()` call does not currently pass `context_files`. Extend it as follows:

```python
# After extracting expected_output and before calling create_task_file:
context_files = body.get("context_files", [])
if not isinstance(context_files, list):
    context_files = []
# Strip whitespace and filter empty strings
context_files = [cf.strip() for cf in context_files if isinstance(cf, str) and cf.strip()]

# Then pass it into create_task_file:
task_path = create_task_file(
    inbox_path=inbox_path,
    task_type=task_type,
    description=description,
    expected_output=expected_output,
    priority=priority,
    created_by="dashboard",
    assigned_to="orchestrator",
    context_files=context_files,   # <-- add this line
)
```

---

## 3. `dashboard/templates/index.html`

### Add the context-files picker block inside the submit form

Insert the following HTML block **between** the `#task-expected-output` form-group and the `.form-actions` div (roughly after the closing `</div>` of the expected-output group, before `<div class="form-actions">`):

```html
<div class="form-group">
    <label for="context-search">Context Files <span class="label-hint">(optional — attach prior results)</span></label>
    <div class="context-picker">
        <input
            type="text"
            id="context-search"
            class="form-input context-search-input"
            placeholder="Search completed tasks by description..."
            autocomplete="off"
        />
        <div id="context-dropdown" class="context-dropdown" style="display:none;">
            <div id="context-dropdown-list" class="context-dropdown-list">
                <p class="context-no-results">Type to search completed tasks</p>
            </div>
        </div>
    </div>
    <div id="context-selected" class="context-selected" style="display:none;">
        <!-- Chips injected here by JS -->
    </div>
</div>
```

No other changes to `index.html`.

---

## 4. `dashboard/static/dashboard.js`

### 4a. Module-level state

Add these two variables near the top of the file, alongside the existing `let pollTimer` declarations:

```js
let completedTasksCache = [];        // populated on first focus of context search
let selectedContextFiles = [];       // array of {task_id, output_path, description_preview}
```

### 4b. New function: `loadCompletedTasks()`

Fetches `/api/tasks/completed` once and populates `completedTasksCache`. Call this lazily on first focus of the search input (see 4d).

```js
async function loadCompletedTasks() {
    if (completedTasksCache.length > 0) return; // already loaded
    try {
        const resp = await fetch('/api/tasks/completed');
        if (resp.ok) {
            completedTasksCache = await resp.json();
        }
    } catch (e) {
        console.warn('Could not load completed tasks:', e);
    }
}
```

### 4c. New function: `renderContextDropdown(query)`

Filters `completedTasksCache` by `query` (case-insensitive match against `description_preview` and `task_id`) and renders matching items into `#context-dropdown-list`. Already-selected tasks are greyed out.

```js
function renderContextDropdown(query) {
    const list = document.getElementById('context-dropdown-list');
    const dropdown = document.getElementById('context-dropdown');

    const q = query.trim().toLowerCase();
    const matches = q
        ? completedTasksCache.filter(t =>
            t.description_preview.toLowerCase().includes(q) ||
            t.task_id.toLowerCase().includes(q)
          )
        : completedTasksCache.slice(0, 20); // show 20 most recent when no query

    if (matches.length === 0) {
        list.innerHTML = '<p class="context-no-results">No matching completed tasks</p>';
    } else {
        list.innerHTML = matches.map(t => {
            const alreadySelected = selectedContextFiles.some(s => s.task_id === t.task_id);
            return `<div
                class="context-dropdown-item${alreadySelected ? ' already-selected' : ''}"
                data-task-id="${t.task_id}"
                data-output-path="${t.output_path}"
                data-preview="${t.description_preview.replace(/"/g, '&quot;')}"
                title="${t.description_preview}"
            >
                <span class="context-item-type">${t.type}</span>
                <span class="context-item-preview">${t.description_preview || t.task_id}</span>
                <span class="context-item-date">${t.created_at ? t.created_at.slice(0, 10) : ''}</span>
            </div>`;
        }).join('');

        // Click handler for each item
        list.querySelectorAll('.context-dropdown-item:not(.already-selected)').forEach(item => {
            item.addEventListener('click', () => {
                addContextFile(item.dataset.taskId, item.dataset.outputPath, item.dataset.preview);
                document.getElementById('context-search').value = '';
                document.getElementById('context-dropdown').style.display = 'none';
            });
        });
    }

    dropdown.style.display = 'block';
}
```

### 4d. New function: `addContextFile(taskId, outputPath, preview)`

Adds an entry to `selectedContextFiles` and re-renders the chips.

```js
function addContextFile(taskId, outputPath, preview) {
    if (selectedContextFiles.some(s => s.task_id === taskId)) return; // deduplicate
    selectedContextFiles.push({ task_id: taskId, output_path: outputPath, description_preview: preview });
    renderContextChips();
}
```

### 4e. New function: `removeContextFile(taskId)`

```js
function removeContextFile(taskId) {
    selectedContextFiles = selectedContextFiles.filter(s => s.task_id !== taskId);
    renderContextChips();
}
```

### 4f. New function: `renderContextChips()`

Renders the selected files as removable chips in `#context-selected`.

```js
function renderContextChips() {
    const container = document.getElementById('context-selected');
    if (selectedContextFiles.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    container.style.display = 'flex';
    container.innerHTML = selectedContextFiles.map(s => `
        <div class="context-chip" title="${s.output_path}">
            <span class="chip-label">${s.description_preview ? s.description_preview.slice(0, 50) : s.task_id}</span>
            <button class="chip-remove" data-task-id="${s.task_id}" aria-label="Remove">&times;</button>
        </div>
    `).join('');

    container.querySelectorAll('.chip-remove').forEach(btn => {
        btn.addEventListener('click', () => removeContextFile(btn.dataset.taskId));
    });
}
```

### 4g. Wire up event listeners in `setupEventListeners()`

Add these inside the existing `setupEventListeners()` function, after the submit form listener block:

```js
// Context files picker
const contextSearch = document.getElementById('context-search');
if (contextSearch) {
    contextSearch.addEventListener('focus', async () => {
        await loadCompletedTasks();
        renderContextDropdown(contextSearch.value);
    });
    contextSearch.addEventListener('input', () => {
        renderContextDropdown(contextSearch.value);
    });
    contextSearch.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.getElementById('context-dropdown').style.display = 'none';
        }
    });
    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.context-picker')) {
            document.getElementById('context-dropdown').style.display = 'none';
        }
    });
}
```

### 4h. Extend `submitTask()` to include `context_files`

In the existing `submitTask()` function, extend the `JSON.stringify(...)` payload to include the selected output paths:

```js
body: JSON.stringify({
    description: description,
    type: type,
    priority: priority,
    expected_output: expectedOutput,
    context_files: selectedContextFiles.map(s => s.output_path),  // <-- add this line
}),
```

Also reset the picker state after a successful submission (in the `if (response.ok)` block, alongside the existing field clears):

```js
// Reset context files picker
selectedContextFiles = [];
renderContextChips();
completedTasksCache = []; // force a fresh fetch next time
```

---

## 5. `dashboard/static/dashboard.css`

Append the following rules at the end of the file. All colours are consistent with the existing palette (`#0066cc`, `#1a1a2e`, `#ddd`).

```css
/* Context Files Picker */
.label-hint {
    font-weight: 400;
    color: #888;
    font-size: 12px;
    margin-left: 4px;
}

.context-picker {
    position: relative;
}

.context-search-input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 13px;
    font-family: inherit;
    background: white;
    color: #333;
    transition: border-color 0.2s ease;
}

.context-search-input:focus {
    outline: none;
    border-color: #0066cc;
    box-shadow: 0 0 0 3px rgba(0, 102, 204, 0.1);
}

.context-dropdown {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: white;
    border: 1px solid #ddd;
    border-radius: 4px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
    z-index: 200;
    max-height: 260px;
    overflow-y: auto;
}

.context-dropdown-list {
    padding: 4px 0;
}

.context-no-results {
    padding: 12px 16px;
    font-size: 13px;
    color: #888;
}

.context-dropdown-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    cursor: pointer;
    font-size: 13px;
    transition: background 0.15s ease;
    border-bottom: 1px solid #f0f0f0;
}

.context-dropdown-item:last-child {
    border-bottom: none;
}

.context-dropdown-item:hover {
    background: #f0f6ff;
}

.context-dropdown-item.already-selected {
    opacity: 0.4;
    cursor: default;
}

.context-item-type {
    background: #e0e7ff;
    color: #3b4fd8;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 3px;
    white-space: nowrap;
    flex-shrink: 0;
}

.context-item-preview {
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #333;
}

.context-item-date {
    font-size: 11px;
    color: #aaa;
    white-space: nowrap;
    flex-shrink: 0;
}

/* Selected context file chips */
.context-selected {
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 10px;
}

.context-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #e8f4ff;
    border: 1px solid #4facfe;
    border-radius: 20px;
    padding: 4px 10px 4px 12px;
    font-size: 12px;
    color: #0066cc;
    max-width: 280px;
}

.chip-label {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.chip-remove {
    background: none;
    border: none;
    color: #0066cc;
    font-size: 16px;
    line-height: 1;
    cursor: pointer;
    padding: 0;
    flex-shrink: 0;
    opacity: 0.7;
    transition: opacity 0.15s;
}

.chip-remove:hover {
    opacity: 1;
}
```

---

## Behaviour Summary

- On first focus of the search input, the picker fetches `/api/tasks/completed` (lazy load, cached in memory).
- Typing filters the dropdown by description or task ID (case-insensitive).
- With an empty search box, the 20 most recent completed tasks are shown.
- Clicking an item adds it as a chip below the input. Already-selected items are dimmed and non-clickable.
- Clicking × on a chip removes it.
- On successful submission, the chip list and cache are reset.
- The dropdown closes on Escape or any click outside `.context-picker`.
- If no context files are selected, `context_files: []` is sent in the payload — `app.py` already handles an empty list gracefully (passes it through to `create_task_file()`).

---

## Critical path constraint

`context_files` paths **must not be altered** anywhere between `task_monitor.py` and `create_task_file()`. The `output_path` values in the monitor are already correct Windows paths (e.g. `C:\Users\JAAS\Desktop\AI Team\outbox\task_xxx_result.md`). They are read from task frontmatter, passed verbatim through the API JSON response, held in `selectedContextFiles[].output_path` in the browser, sent as-is in the POST body, and passed directly to `create_task_file(context_files=[...])`. Any normalisation, `Path(...).resolve()`, or URL-encoding at any stage will break them.
