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
- **Pending**: Tasks awaiting processing
- **Processing**: Currently active tasks
- **Completed**: Successfully finished tasks
- **Failed**: Tasks that errored or QA rejected

Real-time update every 1.5 seconds.

### Active Tasks Tab
View all tasks currently being processed by agents. Shows:
- Task ID (unique identifier)
- Task type (code, research, summarize, etc)
- Priority (high, medium, low)
- Creation timestamp and age
- Creator and assigned agent
- Retry count (if retried)

Click any task to see full details, logs, and results.

### History Tab
Browse completed and failed tasks. Filter by status:
- **Completed**: Successfully finished
- **Failed**: Execution errors or QA rejections

Sorted newest-first, limited to 50 most recent.

### Agent Stats Tab
Per-agent statistics showing:
- Tasks completed
- Error count

Updated in real-time to track agent performance.

### Logs Tab
View agent execution logs. Select any agent:
- orchestrator
- coder
- research
- qa
- claude-code
- scheduler

Shows last 50 log lines, auto-scrolls to bottom on update.

### Task Details Modal
Click any task to open detailed view:

**Metadata**:
- Type, priority, status, location
- Creator, assigned agent
- Creation time, age
- Retry history

**Logs**:
All timestamped log entries for this task across all agents. Shows:
- Timestamp
- Log level (INFO, WARN, ERROR)
- Agent name
- Log message

**Result**:
First 1000 characters of task result file (if exists). For completed tasks, shows agent output. For failed tasks, shows error details.

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
    "failed": 1
  },
  "ollama_lock": {
    "pid": 12345,
    "timestamp": 1234567890
  },
  "agent_stats": {
    "orchestrator": {"completed": 10, "errors": 0},
    "coder": {"completed": 8, "errors": 1},
    ...
  }
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
Per-agent statistics.

**Response:**
```json
{
  "orchestrator": {"completed": 10, "errors": 0},
  "coder": {"completed": 8, "errors": 1},
  "research": {"completed": 12, "errors": 0},
  "qa": {"completed": 8, "errors": 0},
  "claude-code": {"completed": 2, "errors": 0}
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
