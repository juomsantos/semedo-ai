"""
app.py — Flask REST API server for real-time task monitoring dashboard.

Endpoints:
  GET /api/status          - System status and metrics
  GET /api/tasks           - All tasks with pagination/filtering
  GET /api/tasks/<id>      - Task detail with logs and result
  GET /api/agents          - Per-agent statistics
  GET /api/agents/<name>/logs  - Agent logs
  GET /                    - Serve dashboard UI
"""

import sys
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Add dashboard to path
dashboard_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(dashboard_dir))

from task_monitor import TaskMonitor

# Determine project root (parent of dashboard)
PROJECT_ROOT = dashboard_dir.parent

# Initialize Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# Initialize task monitor
monitor = TaskMonitor(PROJECT_ROOT)


@app.route("/")
def index():
    """Serve dashboard UI."""
    return send_from_directory("templates", "index.html")


@app.route("/api/status")
def get_status():
    """Get system status and metrics."""
    try:
        status = monitor.get_system_status()
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks")
def get_tasks():
    """Get all tasks with optional filtering."""
    try:
        limit = request.args.get("limit", 100, type=int)
        status_filter = request.args.get("status", None)
        task_type = request.args.get("type", None)
        
        tasks = monitor.get_all_tasks(limit=limit)
        
        # Apply filters
        if status_filter:
            tasks = [t for t in tasks if t["status"] == status_filter]
        if task_type:
            tasks = [t for t in tasks if t["type"] == task_type]
        
        return jsonify({"tasks": tasks, "count": len(tasks)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/<task_id>")
def get_task_detail(task_id):
    """Get complete task details including result and logs."""
    try:
        task = monitor.get_task_detail(task_id)
        if not task:
            return jsonify({"error": f"Task {task_id} not found"}), 404
        
        return jsonify(task), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agents")
def get_agents():
    """Get per-agent statistics."""
    try:
        stats = monitor.get_agent_stats()
        return jsonify(stats), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agents/<agent>/logs")
def get_agent_logs(agent):
    """Get recent logs for a specific agent."""
    try:
        lines = request.args.get("lines", 50, type=int)
        logs = monitor.get_agent_logs(agent, lines=lines)
        return jsonify({"agent": agent, "logs": logs}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return jsonify({"error": "Internal server error"}), 500


def main(port: int = 5000, debug: bool = False):
    """Run the Flask app."""
    print(f"\n{'='*60}")
    print("AI Team Dashboard — Starting")
    print(f"{'='*60}")
    print(f"Dashboard available at: http://localhost:{port}")
    print(f"API endpoints:")
    print(f"  GET /api/status          - System metrics")
    print(f"  GET /api/tasks           - All tasks")
    print(f"  GET /api/tasks/<id>      - Task detail")
    print(f"  GET /api/agents          - Agent statistics")
    print(f"  GET /api/agents/<name>/logs - Agent logs")
    print()
    
    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()
