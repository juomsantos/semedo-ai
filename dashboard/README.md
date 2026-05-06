# AI Team Dashboard

Real-time monitoring dashboard for the AI Team task processing system.

## Features

- **Real-time Task Monitoring**: Polls every 1.5 seconds for task status updates
- **System Metrics**: View pending, processing, completed, and failed task counts
- **Active Tasks**: See currently processing tasks with details
- **Task History**: Browse completed and failed tasks with filtering
- **Agent Statistics**: Monitor performance metrics per agent
- **Live Logs**: View real-time logs from any agent

## Quick Start

```bash
# Install dependencies
pip install flask flask-cors

# Start the dashboard
python dashboard/run_dashboard.py --port 5000

# Access the dashboard
# Open http://localhost:5000 in your browser
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI |
| `GET /api/status` | System status and metrics |
| `GET /api/tasks` | All tasks with optional filtering |
| `GET /api/tasks/<id>` | Task detail with logs and result |
| `GET /api/agents` | Per-agent statistics |
| `GET /api/agents/<name>/logs` | Agent logs |

## Configuration

Edit `config.json` to customize:
- Dashboard port
- Debug mode
- Polling interval
- Task limits

## Architecture

```
dashboard/
├── app.py              # Flask server with REST API
├── run_dashboard.py    # Launcher script
├── task_monitor.py     # File system scanner
├── templates/
│   └── index.html      # Dashboard UI
└── static/
    ├── dashboard.js    # Frontend logic
    └── dashboard.css   # Styling
```

## How It Works

1. **Backend** (`app.py`): Flask server exposing REST API endpoints
2. **Task Monitor** (`task_monitor.py`): Scans file system for task files
3. **Frontend** (`index.html` + `dashboard.js`): Real-time polling UI
4. **Polling**: JavaScript polls API every 1.5 seconds for updates

## Task File Format

Tasks use YAML frontmatter:

```yaml
---
id: task-xxx
type: coding
priority: high
created_by: user
created_at: 2026-05-06T12:00:00Z
assigned_to: coder
retry_count: 0
output_path: output/xxx
---

Task content here...
```

## Troubleshooting

- **Dashboard not loading**: Check if Flask server is running
- **No tasks showing**: Ensure task files exist in `inbox/` or `agents/*/inbox/`
- **Polling errors**: Check browser console for API errors

## License

MIT
