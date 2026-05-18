"""
app.py — Flask REST API server for real-time task monitoring dashboard.

Endpoints:
  GET /api/status          - System status and metrics
  GET /api/tasks           - All tasks with pagination/filtering
  GET /api/tasks/<id>      - Task detail with logs and result
  GET /api/tasks/completed - Completed parent tasks for context file selection
  GET /api/agents          - Per-agent statistics
  GET /api/agents/<name>/logs  - Agent logs
  POST /api/clear-cache    - Clear all cached data (task files, logs, tokens)
  GET /                    - Serve dashboard UI
"""

import os
import sys
import secrets
import shutil
import requests as _requests
import re
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory, render_template
from werkzeug.utils import secure_filename
from flask_cors import CORS

# Add dashboard to path
dashboard_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(dashboard_dir))

from task_monitor import TaskMonitor
from chat_session import ChatSessionStore
from chat_context import build_base_snapshot, get_deep_task_context, extract_task_id
from agent_chat import call_chat_with_tools
from shared.ollama_client import OllamaError

# Determine project root (parent of dashboard)
PROJECT_ROOT = dashboard_dir.parent

# Add scripts to path for task_io import
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from shared.task_io import create_task_file
from shared.config import load_config as _load_config

# Initialize Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")

# CORS is restricted to loopback origins. The dashboard exposes destructive
# state-changing endpoints (approve/reject/submit), so allowing arbitrary
# origins would enable CSRF from any page the user visits while the dashboard
# is open. Loopback-only is a necessary but not sufficient guardrail — any
# other process running on 127.0.0.1 (a dev server on a different port, a
# malicious local script) can still hit these endpoints. The shared-secret
# token below is the second layer that closes that gap.
_LOOPBACK_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")
CORS(app, origins=_LOOPBACK_ORIGIN_RE, supports_credentials=True)

# ---------------------------------------------------------------------------
# Shared-secret token for state-changing endpoints
# ---------------------------------------------------------------------------
# The token is read from $DASHBOARD_TOKEN if set; otherwise a fresh random
# token is generated at startup. It is injected into the dashboard HTML as a
# <meta name="dashboard-token"> tag, and the JS attaches it as an
# X-Dashboard-Token header on every POST/DELETE call. A request without the
# header (or with a stale one — e.g. after a restart) gets 401.
#
# Regenerating on each startup means open tabs need a refresh after the
# server restarts. That is a deliberate trade-off vs. persisting the token
# to disk: the in-memory approach has no on-disk secret to leak, and the
# refresh is cheap. Set $DASHBOARD_TOKEN in the environment if you want the
# token to survive restarts (e.g. when running under a process supervisor).
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN") or secrets.token_urlsafe(32)


def require_dashboard_token(view):
    """Reject requests that don't carry the matching X-Dashboard-Token header.

    Uses ``secrets.compare_digest`` for constant-time comparison so a remote
    attacker can't time the comparison to learn the token byte-by-byte. CORS
    preflight (OPTIONS) is allowed through; Flask-CORS handles it before this
    decorator runs in normal flow, but checking explicitly is cheap insurance.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return view(*args, **kwargs)
        supplied = request.headers.get("X-Dashboard-Token", "")
        if not supplied or not secrets.compare_digest(supplied, DASHBOARD_TOKEN):
            return jsonify({"error": "unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapper

# Initialize task monitor
monitor = TaskMonitor(PROJECT_ROOT)

# Initialize chat components
chat_session_store = ChatSessionStore(max_history_turns=20)

# Load chat system prompt template
chat_system_prompt_path = dashboard_dir / "chat_system_prompt.md"
CHAT_SYSTEM_PROMPT_TEMPLATE = ""
if chat_system_prompt_path.exists():
    CHAT_SYSTEM_PROMPT_TEMPLATE = chat_system_prompt_path.read_text(encoding='utf-8')

# Defaults used both as the inner fallback (when config.json omits a key)
# and as the outer fallback (when load_config itself blows up). Hoisted to
# module-top so the two paths can't drift.
DEFAULT_CHAT_MODEL = "qwen3.5:9b"
DEFAULT_CHAT_TIMEOUT_S = 120
DEFAULT_CHAT_MAX_TOOL_TURNS = 8
DEFAULT_RAG_BASE_URL = "http://localhost:8000"

# Load config for chat settings
try:
    config = _load_config()
    chat_config = config._config.get("chat", {})
    CHAT_MODEL = chat_config.get("model", config.agent_model("orchestrator") or DEFAULT_CHAT_MODEL)
    CHAT_TIMEOUT = chat_config.get("timeout", DEFAULT_CHAT_TIMEOUT_S)
    CHAT_MAX_TOOL_TURNS = chat_config.get("max_tool_turns", DEFAULT_CHAT_MAX_TOOL_TURNS)
except Exception:
    CHAT_MODEL = DEFAULT_CHAT_MODEL
    CHAT_TIMEOUT = DEFAULT_CHAT_TIMEOUT_S
    CHAT_MAX_TOOL_TURNS = DEFAULT_CHAT_MAX_TOOL_TURNS


@app.route("/")
def index():
    """Serve dashboard UI with the per-process token embedded as a meta tag."""
    return render_template("index.html", dashboard_token=DASHBOARD_TOKEN)


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
@require_dashboard_token
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
@require_dashboard_token
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


@app.route("/api/tasks/completed", methods=["GET"])
def get_completed_tasks():
    """Return completed parent tasks available as context files."""
    try:
        tasks = monitor.get_completed_parent_tasks(limit=100)
        return jsonify(tasks), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/submit", methods=["POST"])
@require_dashboard_token
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

        # Extract and validate context_files
        context_files = body.get("context_files", [])
        if not isinstance(context_files, list):
            context_files = []
        # Strip whitespace and filter empty strings
        context_files = [cf.strip() for cf in context_files if isinstance(cf, str) and cf.strip()]

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
            context_files=context_files,
        )

        # Extract task ID from path (filename format: {task_id}.task.md)
        task_id = task_path.stem.replace(".task", "")

        return jsonify({"task_id": task_id, "message": "Task submitted to orchestrator."}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clear-cache", methods=["POST"])
@require_dashboard_token
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


# ---------------------------------------------------------------------------
# Context file upload
# ---------------------------------------------------------------------------

@app.route("/api/upload-context", methods=["POST"])
@require_dashboard_token
def upload_context_files():
    """Upload one or more local files into context/ and return their Windows paths."""
    if "files" not in request.files:
        return jsonify({"error": "No files provided"}), 400

    context_dir = PROJECT_ROOT / "context"
    context_dir.mkdir(exist_ok=True)

    uploaded = []
    errors = []

    for file in request.files.getlist("files"):
        if not file.filename:
            continue
        filename = secure_filename(file.filename)
        if not filename:
            errors.append(f"Invalid filename: {file.filename!r}")
            continue
        dest = context_dir / filename
        # Avoid silent overwrite — append a counter if the name is taken
        counter = 1
        while dest.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            dest = context_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        file.save(str(dest))
        uploaded.append({
            "name": file.filename,
            "saved_as": dest.name,
            "path": str(dest),          # absolute Windows path — safe for context_files
        })

    if not uploaded and errors:
        return jsonify({"error": "; ".join(errors)}), 400

    return jsonify({"uploaded": uploaded, "errors": errors}), 200


# ---------------------------------------------------------------------------
# RAG API proxy endpoints
# ---------------------------------------------------------------------------

def _rag_base_url() -> str:
    try:
        return _load_config().rag_api_url()
    except Exception:
        return DEFAULT_RAG_BASE_URL


@app.route("/api/rag/documents", methods=["GET"])
def rag_list_documents():
    """List all documents in the knowledge base."""
    try:
        resp = _requests.get(f"{_rag_base_url()}/documents", timeout=10)
        return jsonify(resp.json()), resp.status_code
    except _requests.exceptions.ConnectionError:
        return jsonify({"error": "RAG API unavailable"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rag/ingest", methods=["POST"])
@require_dashboard_token
def rag_ingest():
    """Ingest a document into the knowledge base."""
    try:
        body = request.get_json() or {}
        resp = _requests.post(f"{_rag_base_url()}/ingest", json=body, timeout=600)
        return jsonify(resp.json()), resp.status_code
    except _requests.exceptions.ConnectionError:
        return jsonify({"error": "RAG API unavailable"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rag/documents/<doc_id>", methods=["DELETE"])
@require_dashboard_token
def rag_delete_document(doc_id):
    """Delete a document from the knowledge base."""
    try:
        resp = _requests.delete(f"{_rag_base_url()}/documents/{doc_id}", timeout=10)
        return jsonify(resp.json()), resp.status_code
    except _requests.exceptions.ConnectionError:
        return jsonify({"error": "RAG API unavailable"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rag/status", methods=["GET"])
def rag_status():
    """Check RAG API liveness."""
    try:
        resp = _requests.get(f"{_rag_base_url()}/health", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except _requests.exceptions.ConnectionError:
        return jsonify({"status": "unavailable"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
@require_dashboard_token
def chat():
    """Chat endpoint with LLM and tools."""
    try:
        body = request.get_json() or {}
        user_message = body.get("message", "").strip()
        session_id = body.get("session_id")
        client_timestamp = body.get("timestamp", "").strip()  # ISO string from browser

        if not user_message:
            return jsonify({"error": "message is required"}), 400

        # Create or retrieve session
        if not session_id:
            session_id = chat_session_store.new_session()

        history = chat_session_store.get_history(session_id)

        # Build system prompt — inject current server datetime
        base_snapshot = build_base_snapshot(PROJECT_ROOT)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.replace("{PIPELINE_SNAPSHOT}", base_snapshot).replace("{NOW}", now_str)

        # Prefix user message with timestamp for history (so the LLM can see timing)
        ts_label = client_timestamp if client_timestamp else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        stamped_user_message = f"[{ts_label}] {user_message}"

        # Check if message mentions a task ID and inject deep context
        task_id = extract_task_id(user_message)
        if task_id:
            deep_context = get_deep_task_context(task_id, PROJECT_ROOT)
            system_prompt = system_prompt.replace("{PIPELINE_SNAPSHOT}", f"{base_snapshot}{deep_context}")

        # Call LLM with tools — pass the timestamped message so the model sees timing
        try:
            reply = call_chat_with_tools(
                model=CHAT_MODEL,
                system_prompt=system_prompt,
                history=history,
                user_message=stamped_user_message,
                max_tool_turns=CHAT_MAX_TOOL_TURNS,
            )
        except OllamaError as e:
            return jsonify({"error": f"LLM error: {str(e)}"}), 503

        # Parse CREATE_TASK block if present
        action = None
        task_match = re.search(r'<CREATE_TASK>\s*(\{[^}]+\})\s*</CREATE_TASK>', reply, re.DOTALL)
        if task_match:
            import json
            try:
                task_data = json.loads(task_match.group(1))
                # Create task
                task_type = task_data.get("type", "code").strip()
                priority = task_data.get("priority", "medium").strip()
                description = task_data.get("description", "").strip()
                expected_output = task_data.get("expected_output", "See task description.").strip()

                if description:
                    inbox_path = PROJECT_ROOT / "inbox"
                    task_path = create_task_file(
                        inbox_path=inbox_path,
                        task_type=task_type,
                        description=description,
                        expected_output=expected_output,
                        priority=priority,
                        created_by="chat",
                        assigned_to="orchestrator",
                        context_files=[],
                    )
                    new_task_id = task_path.stem.replace(".task", "")
                    action = {"type": "task_created", "task_id": new_task_id}

                # Strip the CREATE_TASK block from reply
                reply = re.sub(r'<CREATE_TASK>.*?</CREATE_TASK>', '', reply, flags=re.DOTALL).strip()
            except (json.JSONDecodeError, Exception):
                pass

        # Append to history — store the timestamped version so future turns also see timing
        chat_session_store.append(session_id, "user", stamped_user_message)
        chat_session_store.append(session_id, "assistant", reply)

        result = {
            "reply": reply,
            "session_id": session_id,
        }
        if action:
            result["action"] = action

        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat/clear", methods=["POST"])
@require_dashboard_token
def clear_chat():
    """Clear chat history for a session."""
    try:
        body = request.get_json() or {}
        session_id = body.get("session_id")

        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        chat_session_store.clear(session_id)
        return jsonify({"status": "cleared"}), 200

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


def main(port: int = 5000, debug: bool = False, host: str = "127.0.0.1"):
    """Run the Flask app.

    `host` defaults to loopback only — the dashboard has no auth and exposes
    destructive endpoints (approve/reject/submit), so it must not be reachable
    from outside the local machine. If you really need LAN/remote access, put
    the dashboard behind a reverse proxy with authentication and explicitly
    pass `host="0.0.0.0"` (or a specific interface IP).
    """
    print(f"\n{'='*60}")
    print("AI Team Dashboard — Starting")
    print(f"{'='*60}")
    print(f"Dashboard available at: http://{host}:{port}")
    print(f"API endpoints:")
    print(f"  GET /api/status          - System metrics")
    print(f"  GET /api/tasks           - All tasks")
    print(f"  GET /api/tasks/<id>      - Task detail")
    print(f"  GET /api/tasks/completed - Completed tasks for context")
    print(f"  GET /api/agents          - Agent statistics")
    print(f"  GET /api/agents/<name>/logs - Agent logs")
    print(f"  GET /api/pending-approvals - Tasks awaiting approval")
    print(f"  POST /api/pending-approvals/<id>/approve - Approve task")
    print(f"  POST /api/pending-approvals/<id>/reject - Reject task")
    print(f"  POST /api/tasks/submit   - Submit new task")
    print(f"  POST /api/clear-cache    - Clear all cached data")
    if not os.environ.get("DASHBOARD_TOKEN"):
        # We auto-generated the token. Print it so callers running outside the
        # browser (curl, scripts) can read it. The browser itself doesn't need
        # this — the token is embedded in the served HTML.
        print()
        print(f"  Dashboard token (auto-generated): {DASHBOARD_TOKEN}")
        print(f"  Set $DASHBOARD_TOKEN to make this stable across restarts.")
    print()

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
