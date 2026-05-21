# AI Team Dashboard — Real-time Task Monitoring

The Dashboard provides a real-time web UI for monitoring the AI Team multi-agent system. View task statuses, agent statistics, execution logs, and system metrics in a single browser window.

## Quick Start

### Prerequisites
- Flask: `pip install flask flask-cors`
- (Flask is already in most Python environments, but install if needed)

### Run Dashboard

```bash
python dashboard/run_dashboard.py
```

Or specify port:
```bash
python dashboard/run_dashboard.py --port 8000
```

Or enable debug mode:
```bash
python dashboard/run_dashboard.py --debug
```

Dashboard will be available at: **http://localhost:5000** (or your custom port)

## Configuration

Settings in `config.json` under `dashboard` section:

```json
{
  "dashboard": {
    "port": 5000,           // Port to listen on
    "debug": false,         // Flask debug mode
    "poll_interval": 1500   // Frontend poll interval (milliseconds)
  }
}
```

## Features

### System Status Panel
- **Pending**: Tasks awaiting processing (inbox + all worker inboxes)
- **Awaiting Approval**: Tasks in `agents/claude-code/pending/` awaiting your decision
- **Processing**: Tasks currently being worked on
- **Completed**: Successfully finished tasks (in `outbox/`)
- **Failed**: Tasks that errored or QA rejected (in `failed/`)

Real-time update every 1.5 seconds.

### Active Tasks Tab
View all tasks currently being processed by agents. Shows task ID, type, priority, age, creator, and assigned agent. Retry count shown if the task has been retried. Click any task to open the details modal.

### Approvals Tab
Shows all tasks in `agents/claude-code/pending/` — tasks the orchestrator routed to claude-code that require manual approval before running. A badge on the tab shows the count when tasks are waiting.

Click a task card to open a detail modal showing all metadata fields (type, priority, created_by, etc.) and the full task body. Each task card also has **Approve** and **Reject** buttons:
- **Approve** — moves the task to `agents/claude-code/inbox/`; the claude-code agent picks it up on its next poll
- **Reject** — prompts for a rejection reason, then moves the task to `failed/` with the reason appended

### History Tab
Browse all tasks across every pipeline stage (Active, Validating, Completed, Failed). Filter by status (All / Completed / Failed). Sorted newest-first, limited to 50 most recent. Click any task card to open the full details modal.

### Agent Stats Tab
Per-agent statistics showing:
- **Completed** — tasks finished (parsed from log files)
- **Errors** — error count from logs
- **Prompt Tokens** — cumulative input tokens (from `logs/<agent>/tokens.jsonl`)
- **Completion Tokens** — cumulative output tokens
- **LLM Calls** — total Ollama calls

`claude-code` shows an approximate completion token count (word-count proxy) and `0` prompt tokens — the Claude CLI does not report token counts. These values are intentional approximations (M7).

### Logs Tab
View agent execution logs. Select any agent (orchestrator, coder, research, qa, claude-code, scheduler). Shows last 50 log lines in **newest-first** order — most recent entry at the top.

### Knowledge Base Tab
Manage the local RAG knowledge base (requires the scheduler to be running, which starts the RAG API automatically).

**Status badge:** shows whether the RAG API is Online or Unavailable. If unavailable, start the scheduler.

**Add Document panel:**
- **Title** — human-readable label for the document
- **Source** (optional) — file path, URL, or any reference string
- **Content** — paste any text: documentation, architecture notes, code snippets, prior results, etc.

Click **Add to Knowledge Base** to chunk, embed, and store the content. The response shows how many chunks were created.

**Stored Documents** — lists all documents currently in the vector store with their title and source. Click **✕** to delete a document permanently.

Documents added here are immediately available to agents — the next task that triggers `rag_query` will find them.

### Submit Task Tab
Submit a new task to the orchestrator directly from the dashboard without touching the filesystem.

Fields:
- **Type** — code | research | summarize | review | plan (default: code)
- **Priority** — medium | high | low (default: medium)
- **Description** — what to build or research (required)
- **Expected Output** — what a correct result looks like (optional)

On submit, creates a `.task.md` file in `inbox/` and returns the task ID. The file watcher detects it immediately and triggers the orchestrator; the new task typically appears in the Active Tasks tab within 1–2 seconds. Type and priority fields retain their values for quick follow-up submissions.

### Chat Assistant Tab

An embedded LLM chat interface powered by `qwen3.5:9b` with live pipeline awareness and tool access. Responses stream token-by-token to the browser via Server-Sent Events. It can answer status questions, look up task details, search the knowledge base and web, and create new tasks — all from a single chat window.

**Capabilities:**
- **Pipeline status** — "What's processing?", "How many tasks failed?", "What's in the inbox?"
- **Task details** — mention a task ID (e.g. `task_20260516_120000_123456`) and the assistant auto-injects full metadata, body, result, and relevant log lines as deep context
- **Knowledge base queries** — searches `http://localhost:8000` via `rag_query` for prior results, architecture notes, and project documentation
- **Web research** — `web_search` (up to 5 results) and `web_fetch` (full page content) via the Ollama web API
- **Task creation** — ask the assistant to create a task; it emits a `<CREATE_TASK>` block which the backend parses and submits to `inbox/` automatically

**Creating tasks via chat:**

Ask naturally ("research the best Python HTTP client libraries" or "create a code task to add pagination to the API"). The assistant describes what it's creating, then the backend extracts the task and submits it to the orchestrator. A green confirmation badge with the new task ID appears below the response.

**Streaming responses:**

Chat uses `/api/chat/stream` (SSE) instead of a blocking request. The response bubble appears immediately with a pulsing `···` indicator while the backend runs the tool loop. Tool calls are shown as small badges (📚 rag query, 🔍 web search, 🌐 web fetch) before any text arrives. Tokens stream in as they are generated; a blinking cursor marks the live position. When the stream ends the raw text is converted to rendered markdown with syntax-highlighted code blocks.

**Thinking mode toggle:**

A toggle button next to Send switches between two modes:

| Toggle | Label | LLM behaviour | Use for |
|--------|-------|---------------|---------|
| Off | ⚡ Standard | `think: False` — fast, direct answers | Pipeline status, quick lookups |
| On  | 🧠 Thinking | `think: True`  — full chain-of-thought reasoning | Debugging, architecture questions, complex analysis |

When thinking mode is active and the model produces a reasoning trace, a collapsible **💭 Thinking** block appears above the response. Click it to expand and read the model's internal reasoning. Different sampling options are applied per mode (configured in `config.json`).

**Markdown rendering:** assistant responses are rendered as HTML using `marked.js` (GFM mode). Code blocks are syntax-highlighted via `highlight.js`. User messages are displayed as plain text.

**Session behaviour:** conversation history is stored in memory (not persisted across dashboard restarts). Each browser session gets a UUID; up to 20 turns of history are retained (oldest pairs are dropped when the limit is hit). The **Clear Chat** button resets the current session.

**Limitations:**
- Cannot modify, approve, or reject existing tasks (use the **Approvals** tab for claude-code tasks)
- Cannot delete files or clear the cache
- Session history is lost when the dashboard process restarts

**Configuration** (`config.json → chat`):

```json
{
  "chat": {
    "model": "qwen3.5:9b",
    "timeout": 240,
    "max_history_turns": 20,
    "max_tool_turns": 8,
    "options_standard": {
      "temperature": 0.7,
      "top_p": 0.8,
      "top_k": 20,
      "presence_penalty": 1.5,
      "repeat_penalty": 1.0,
      "num_ctx": 32768
    },
    "options_thinking": {
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": 20,
      "presence_penalty": 1.5,
      "repeat_penalty": 1.0,
      "num_ctx": 32768
    }
  }
}
```

`options_standard` is used when the ⚡ Standard toggle is active; `options_thinking` is used when 🧠 Thinking is active. The `think` parameter sent to Ollama mirrors the toggle state. Both option sets fall back to hard-coded defaults if absent from `config.json`.

### Task Hierarchy View
The History tab renders tasks in a parent/child tree. Parent tasks expand to show their subtasks (coder, research, QA, retry coders) in the order they were created. This makes it easy to trace the full lifecycle of a request — which subtasks were created, whether QA triggered a retry, and which iteration completed successfully.

### Clear Cached Data
A **Clear Cached Data** button (available in the dashboard) calls `POST /api/clear-cache`. This deletes all task files across every pipeline folder, clears all agent log files, and resets token counters. Use for a clean slate between test runs. **Irreversible** — all task history and results are permanently deleted.

### Task Details Modal
Click any task card to open the detailed view:

**Metadata**: type, priority, status, location, creator, assigned agent, creation time, age, retry history.

**Task Body**: the full task description and expected output as originally submitted.

**Logs**: all timestamped log entries for this task across all agents (timestamp, level, agent, message).

**Result**: the complete result file content, if it exists.

## REST API

The dashboard exposes REST endpoints for programmatic access:

### GET /api/status
System status and metrics.

**Response:**
```json
{
  "timestamp": "2026-05-06T11:00:00Z",
  "counts": {
    "pending": 5,
    "processing": 2,
    "completed": 42,
    "failed": 1,
    "awaiting_approval": 1
  },
  "ollama_lock": {
    "pid": 12345,
    "timestamp": 1234567890
  },
  "agent_stats": { ... }
}
```

### GET /api/tasks
List all tasks with optional filtering.

**Query Parameters:**
- `limit` (int, default 100): Max tasks to return
- `status` (string): Filter by status (pending, processing, completed, failed)
- `type` (string): Filter by task type (code, research, etc)

**Response:**
```json
{
  "tasks": [
    {
      "id": "task_20260506_110000",
      "type": "code",
      "priority": "high",
      "created_by": "claude-cowork",
      "created_at": "2026-05-06T11:00:00Z",
      "assigned_to": "coder",
      "status": "processing",
      "location": "processing",
      "retry_count": 0,
      "chain_to": "qa",
      "age_seconds": 125,
      "body_preview": "Write a Python function that..."
    },
    ...
  ],
  "count": 15
}
```

### GET /api/tasks/:id
Full details for a specific task.

**Response:**
```json
{
  "id": "task_20260506_110000",
  "type": "code",
  "priority": "high",
  ...
  "body": "## Task Description\nWrite a Python function...",
  "logs": [
    {
      "timestamp": "2026-05-06T11:00:01Z",
      "level": "INFO",
      "agent": "orchestrator",
      "message": "Processing task..."
    },
    ...
  ],
  "result": "Full result text..."
}
```

### GET /api/agents
Per-agent statistics including token usage.

**Response:**
```json
{
  "orchestrator": {"completed": 10, "errors": 0, "prompt_tokens": 4821, "completion_tokens": 1203, "llm_calls": 14},
  "coder":        {"completed": 8,  "errors": 1, "prompt_tokens": 3102, "completion_tokens": 892,  "llm_calls": 9},
  "research":     {"completed": 12, "errors": 0, "prompt_tokens": 7431, "completion_tokens": 2104, "llm_calls": 12},
  "qa":           {"completed": 8,  "errors": 0, "prompt_tokens": 5210, "completion_tokens": 1441, "llm_calls": 8},
  "claude-code":  {"completed": 2,  "errors": 0, "prompt_tokens": 0,    "completion_tokens": 0,    "llm_calls": 0}
}
```

Token counts are read from `logs/<agent>/tokens.jsonl` and are cumulative across all runs.

### GET /api/pending-approvals
List all tasks in `agents/claude-code/pending/`.

**Response:**
```json
{
  "tasks": [
    {
      "id": "task_20260507_110000_000000",
      "type": "code",
      "priority": "medium",
      "created_by": "orchestrator",
      "created_at": "2026-05-07T11:00:00",
      "assigned_to": "pending_approval",
      "status": "pending_approval",
      "age_seconds": 120,
      "body": "## Task Description\n..."
    }
  ],
  "count": 1
}
```

### POST /api/pending-approvals/:id/approve
Move a pending task to `agents/claude-code/inbox/`. Updates `status: pending` in frontmatter.

**Response:** `{"status": "approved", "task_id": "..."}`

### POST /api/pending-approvals/:id/reject
Move a pending task to `failed/` with the rejection reason appended to the task body.

**Request body (optional):** `{"reason": "Not safe to run"}`

**Response:** `{"status": "rejected", "task_id": "..."}`

### POST /api/tasks/submit
Create a new task in `inbox/` (submits to the orchestrator).

**Request body:**
```json
{
  "description": "Write a Python script that...",
  "type": "code",
  "priority": "medium",
  "expected_output": "A working script that..."
}
```
`type` must be one of: `code`, `research`, `summarize`, `review`, `plan`. `priority` must be `high`, `medium`, or `low`. `description` is required; `expected_output` defaults to `"See task description."` if omitted.

**Response (201):** `{"task_id": "task_20260507_...", "message": "Task submitted to orchestrator."}`

**Error (400):** `{"error": "description is required"}`

### GET /api/rag/status
Check RAG API liveness.

**Response (200):** `{"status": "ok"}` or similar health payload from the RAG API.

**Response (503):** `{"status": "unavailable"}` when the RAG API process is not running.

### GET /api/rag/documents
List all documents in the knowledge base.

**Response:** passes through the RAG API's `/documents` response (list of `{id, metadata}` objects).

### POST /api/rag/ingest
Add a document to the knowledge base. Proxied to `POST /ingest` on the RAG API.

**Request body:**
```json
{
  "content": "Text to embed and store...",
  "metadata": { "title": "My Doc", "source": "architecture.md" }
}
```

**Response:** passes through RAG API response (includes `chunks_created` or `document_ids`).

### DELETE /api/rag/documents/:id
Remove a document by ID.

**Response:** passes through RAG API delete response.

### POST /api/chat
Send a message to the chat assistant.

**Request body:**
```json
{
  "message": "What tasks are currently processing?",
  "session_id": "optional-uuid-from-prior-response"
}
```

If `session_id` is omitted, a new session is created and its ID is returned for subsequent requests.

When the message mentions a task ID (e.g. `task_20260516_120000_123456`), the system auto-injects deep context (full task body, result file, and relevant log lines) into the LLM's system prompt for that turn.

**Response (200):**
```json
{
  "reply": "There are 2 tasks currently processing: task_... (code) and task_... (research).",
  "session_id": "abc123-uuid",
  "action": {
    "type": "task_created",
    "task_id": "task_20260516_..."
  }
}
```

The `action` field is only present when the assistant created a task. `reply` has the `<CREATE_TASK>` block stripped — it contains only the human-readable response.

**Error (400):** `{"error": "message is required"}`

**Error (503):** `{"error": "LLM error: ..."}` — Ollama unreachable or returned an error.

### POST /api/chat/stream
Send a message and receive the response as a stream of Server-Sent Events. Uses the same session store and tool-calling logic as `/api/chat` but streams tokens to the browser as they are generated.

**Request body:** identical to `POST /api/chat`.

**Response:** `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`. Each event is a `data: {json}\n\n` line. Event types:

| `type` | Additional fields | Description |
|--------|-------------------|-------------|
| `meta` | `session_id` | First event — provides the session UUID |
| `tool_call` | `name`, `args` | A tool was dispatched (rag_query / web_search / web_fetch) |
| `thinking` | `text` | Chunk of model reasoning (thinking mode only) |
| `token` | `text` | Content token from the final LLM response |
| `done` | `full_content`, `action?` | Stream complete; `full_content` is the assembled text; `action` present only when a task was created |
| `error` | `message` | LLM or tool error; stream ends after this event |

The terminal `data: [DONE]\n\n` line (no JSON) signals end of stream.

**Note:** Not wrapped in `@_json_error_envelope` — errors are delivered as `{"type": "error", ...}` SSE events, not as HTTP error responses.

### POST /api/chat/clear
Clear conversation history for a session.

**Request body:**
```json
{ "session_id": "abc123-uuid" }
```

**Response (200):** `{"status": "cleared", "session_id": "abc123-uuid"}`

**Error (400):** `{"error": "session_id is required"}`

### POST /api/clear-cache
Delete all task files, agent logs, and token counters. Full system reset.

**Response (200):** `{"status": "success"}`

**Error (500):** `{"status": "error", "message": "..."}`

Clears all `.task.md`, `*_result.md`, and `*_qa_failure.md` files from `inbox/`, `processing/`, `validation/`, `outbox/`, `failed/`, and all worker inboxes. Also deletes `logs/<agent>/general.log` and `logs/<agent>/tokens.jsonl` for every agent.

### GET /api/agents/:agent/logs
Recent logs for a specific agent.

**Query Parameters:**
- `lines` (int, default 50): Number of log lines to return

**Response:**
```json
{
  "agent": "coder",
  "logs": [
    "[2026-05-06T11:00:01Z] [INFO] [coder] Starting agent_coder.py",
    "[2026-05-06T11:00:02Z] [INFO] [coder] Found 1 task(s)",
    ...
  ]
}
```

## Real-time Updates

Dashboard polls all endpoints every **1.5 seconds** for real-time updates:
- System metrics refresh
- Task list updates (tasks move between states)
- Agent stats updated
- Log files monitored for new entries

Poll interval can be configured in `config.json` (`dashboard.poll_interval` in milliseconds).

Note: the scheduler uses a file watcher to trigger agents immediately when tasks arrive, so tasks submitted via the dashboard are typically picked up by the orchestrator in under 1 second — well before the dashboard's next poll cycle.

## Data Source

Dashboard reads directly from the file system:
- Task files: `inbox/`, `processing/`, `validation/`, `outbox/`, `failed/`, `agents/*/inbox/`, `agents/claude-code/pending/`
- Logs: `logs/<agent>/general.log`
- Results: `outbox/*_result.md`, `failed/*_result.md`

No database or special files needed. Same data source as the agents themselves.

## Browser Compatibility

Works on all modern browsers:
- Chrome/Chromium 90+
- Firefox 88+
- Safari 14+
- Edge 90+

Responsive design works on desktop and tablet (mobile is supported but crowded).

## Running Alongside Scheduler

Dashboard and scheduler can run independently:

**Terminal 1 - Scheduler:**
```bash
python scripts/scheduler.py
```

**Terminal 2 - Dashboard:**
```bash
python dashboard/run_dashboard.py
```

Or run in background:
```bash
# Windows
start python dashboard/run_dashboard.py

# Linux/Mac
python dashboard/run_dashboard.py &
```

## Troubleshooting

### "Cannot connect to dashboard"
- Check port 5000 is not in use: `netstat -an | grep 5000` (or `netstat -ano` on Windows)
- Try different port: `python dashboard/run_dashboard.py --port 8000`

### "No tasks showing"
- Check that `inbox/`, `processing/`, etc folders exist
- Check that tasks are being created (add a test task)
- Check logs in `logs/` directory

### "Logs not updating"
- Verify log files exist: `logs/<agent>/general.log`
- Check file permissions
- Wait 1-2 seconds for refresh

### High CPU usage
- Dashboard poll interval is 1.5 seconds (reasonable)
- If still high, increase in `config.json`: `"poll_interval": 3000`

## Development

Dashboard code:
- `dashboard/app.py` - Flask REST API server (includes chat, RAG proxy, and approval endpoints)
- `dashboard/task_monitor.py` - File system scanner
- `dashboard/run_dashboard.py` - Launcher script
- `dashboard/templates/index.html` - UI HTML
- `dashboard/static/dashboard.js` - Real-time polling and UI logic
- `dashboard/static/dashboard.css` - Styling
- `dashboard/agent_chat.py` - Chat LLM tool loop: `call_chat_with_tools` (blocking) and `stream_chat_with_tools` (generator — yields SSE-style event dicts; used by `/api/chat/stream`)
- `dashboard/chat_context.py` - Pipeline snapshot builder and deep-task-context injector
- `dashboard/chat_session.py` - In-memory session store (UUID-keyed, max 20 history turns)
- `dashboard/chat_system_prompt.md` - Chat assistant system prompt template (injected with live pipeline state)

To modify:
1. Edit source files
2. Dashboard auto-reloads on change if run with `--debug`
3. Restart to apply changes (no debug mode)
