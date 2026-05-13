"""
app.py — Flask REST API server for real-time task monitoring dashboard.

Endpoints:
  GET /api/status          - System status and metrics
  GET /api/tasks           - All tasks with pagination/filtering
  GET /api/tasks/<id>      - Task detail with logs and result
  GET /api/agents          - Per-agent statistics
  GET /api/agents/<name>/logs  - Agent logs
  POST /api/clear-cache    - Clear all cached data (task files, logs, tokens)
  GET /                    - Serve dashboard UI
"""

import sys
import shutil
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Add dashboard to path
dashboard_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(dashboard_dir))

from task_monitor import TaskMonitor

# Determine project root (parent of dashboard)
PROJECT_ROOT = dashboard_dir.parent

# Add scripts to path for task_io import
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from shared.task_io import create_task_file

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


@app.route("/api/tasks/<task_id>/payload")
def get_task_payload(task_id):
    """Get raw task file content."""
    try:
        payload = monitor.get_task_payload(task_id)
        if not payload:
            return jsonify({"error": f"Task {task_id} not found"}), 404

        return jsonify({"id": task_id, "content": payload}), 200
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


@app.route("/api/pending-approvals")
def get_pending_approvals():
    """Get all tasks awaiting approval."""
    try:
        tasks = monitor.get_pending_approvals()
        return jsonify({"tasks": tasks, "count": len(tasks)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending-approvals/<task_id>/approve", methods=["POST"])
def approve_task(task_id):
    """Approve a pending task."""
    try:
        success = monitor.approve_task(task_id)
        if not success:
            return jsonify({"error": f"Task {task_id} not found"}), 404
        return jsonify({"status": "approved", "task_id": task_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending-approvals/<task_id>/reject", methods=["POST"])
def reject_task(task_id):
    """Reject a pending task."""
    try:
        body = request.get_json() or {}
        reason = body.get("reason", "Rejected by user")
        success = monitor.reject_task(task_id, reason)
        if not success:
            return jsonify({"error": f"Task {task_id} not found"}), 404
        return jsonify({"status": "rejected", "task_id": task_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/results/<agent>")
def get_results(agent):
    """Get completed and failed results for a specific agent."""
    try:
        results = monitor.get_results_by_agent(agent)
        return jsonify(results), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/submit", methods=["POST"])
def submit_task():
    """Submit a new task to the orchestrator."""
    try:
        body = request.get_json() or {}

        # Validate required fields
        description = body.get("description", "").strip()
        if not description:
            return jsonify({"error": "description is required"}), 400

        task_type = body.get("type", "").strip()
        valid_types = ["code", "research", "summarize", "review", "plan"]
        if task_type not in valid_types:
            return jsonify({"error": f"type must be one of: {', '.join(valid_types)}"}), 400

        priority = body.get("priority", "medium").strip()
        valid_priorities = ["high", "medium", "low"]
        if priority not in valid_priorities:
            return jsonify({"error": f"priority must be one of: {', '.join(valid_priorities)}"}), 400

        expected_output = body.get("expected_output", "").strip()
        if not expected_output:
            expected_output = "See task description."

        # Create task file
        inbox_path = PROJECT_ROOT / "inbox"
        task_path = create_task_file(
            inbox_path=inbox_path,
            task_type=task_type,
            description=description,
            expected_output=expected_output,
            priority=priority,
            created_by="dashboard",
            assigned_to="orchestrator",
        )

        # Extract task ID from path (filename format: {task_id}.task.md)
        task_id = task_path.stem.replace(".task", "")

        return jsonify({"task_id": task_id, "message": "Task submitted to orchestrator."}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    """Clear all cached data: task files, logs, and token counters."""
    try:
        # Define folders to clear
        folders_to_clear = [
            PROJECT_ROOT / "inbox",
            PROJECT_ROOT / "processing",
            PROJECT_ROOT / "validation",
            PROJECT_ROOT / "outbox",
            PROJECT_ROOT / "failed",
            PROJECT_ROOT / "agents" / "orchestrator" / "inbox",
            PROJECT_ROOT / "agents" / "coder" / "inbox",
            PROJECT_ROOT / "agents" / "research" / "inbox",
            PROJECT_ROOT / "agents" / "qa" / "inbox",
            PROJECT_ROOT / "agents" / "claude-code" / "inbox",
            PROJECT_ROOT / "agents" / "claude-code" / "pending",
        ]

        # Clear task files
        for folder in folders_to_clear:
            if folder.exists():
                for pattern in ("*.task.md", "*_result.md", "*_qa_failure.md"):
                    for file_path in folder.glob(pattern):
                        file_path.unlink()

        # Clear log files
        log_folders = [
            PROJECT_ROOT / "logs" / "orchestrator",
            PROJECT_ROOT / "logs" / "coder",
            PROJECT_ROOT / "logs" / "research",
            PROJECT_ROOT / "logs" / "qa",
            PROJECT_ROOT / "logs" / "claude-code",
            PROJECT_ROOT / "logs" / "scheduler",
        ]

        for log_folder in log_folders:
            if log_folder.exists():
                log_file = log_folder / "general.log"
                if log_file.exists():
                    log_file.unlink()
                tokens_file = log_folder / "tokens.jsonl"
                if tokens_file.exists():
                    tokens_file.unlink()

        return jsonify({"status": "success"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


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
    print(f"  GET /api/pending-approvals - Tasks awaiting approval")
    print(f"  POST /api/pending-approvals/<id>/approve - Approve task")
    print(f"  POST /api/pending-approvals/<id>/reject - Reject task")
    print(f"  POST /api/tasks/submit   - Submit new task")
    print(f"  POST /api/clear-cache    - Clear all cached data")
    print()

    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()
