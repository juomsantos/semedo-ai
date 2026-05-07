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

### Results Tab
Browse completed and failed task output files grouped by agent. Select any agent from the dropdown (defaults to orchestrator) to see its results split into two sections:
- **✓ Completed** — tasks that reached `outbox/`, with output preview (first 2000 characters), colour-coded in blue
- **✗ Failed** — tasks in `failed/`, colour-coded in pink

Updates every 1.5 seconds. Useful for quickly scanning what the agents produced without leaving the dashboard.

### History Tab
Browse completed and failed tasks. Filter by status (All / Completed / Failed). Sorted newest-first, limited to 50 most recent. Click any task to see full details and result.

### Agent Stats Tab
Per-agent statistics showing:
- **Completed** — tasks finished (parsed from log files)
- **Errors** — error count from logs
- **Prompt Tokens** — cumulative input tokens (from `logs/<agent>/tokens.jsonl`)
- **Completion Tokens** — cumulative output tokens
- **LLM Calls** — total Ollama calls

`claude-code` shows `—` in token columns (it uses the Claude CLI, not Ollama directly).

### Logs Tab
View agent execution logs. Select any agent (orchestrator, coder, research, qa, claude-code, scheduler). Shows last 50 log lines in **newest-first** order — most recent entry at the top.

### Submit Task Tab
Submit a new task to the orchestrator directly from the dashboard without touching the filesystem.

Fields:
- **Type** — code | research | summarize | review | plan (default: code)
- **Priority** — medium | high | low (default: medium)
- **Description** — what to build or research (required)
- **Expected Output** — what a correct result looks like (optional)

On submit, creates a `.task.md` file in `inbox/` and returns the task ID. The new task appears in the Active Tasks tab within ~1.5 seconds. Type and priority fields retain their values for quick follow-up submissions.

### Task Details Modal
Click any task to open the detailed view:

**Metadata**: type, priority, status, location, creator, assigned agent, creation time, age, retry history.

**Logs**: all timestamped log entries for this task across all agents (timestamp, level, agent, message).

**Result**: first 1000 characters of the task's result file, if it exists.

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

### GET /api/results/:agent
Completed and failed task outputs for a specific agent.

**Path parameter:** `agent` — one of `orchestrator`, `coder`, `research`, `qa`, `claude-code`

**Response:**
```json
{
  "completed": [
    {
      "task_id": "task_20260507_142710_831910",
      "preview": "## Result\n...",
      "path": "outbox/task_20260507_142710_831910_result.md"
    }
  ],
  "failed": [
    {
      "task_id": "task_20260507_143716_671698",
      "preview": "## QA Failure\n...",
      "path": "failed/task_20260507_143716_671698_result.md"
    }
  ]
}
```

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

## Data Source

Dashboard reads directly from the file system:
- Task files: `inbox/`, `processing/`, `outbox/`, `failed/`, `agents/*/inbox/`
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
- `dashboard/app.py` - Flask REST API server
- `dashboard/task_monitor.py` - File system scanner
- `dashboard/run_dashboard.py` - Launcher script
- `dashboard/templates/index.html` - UI HTML
- `dashboard/static/dashboard.js` - Real-time polling and UI logic
- `dashboard/static/dashboard.css` - Styling

To modify:
1. Edit source files
2. Dashboard auto-reloads on change if run with `--debug`
3. Restart to apply changes (no debug mode)
