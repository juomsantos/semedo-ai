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
from flask import Flask, jsonify, request, send_from_directory, render_template, Response, stream_with_context
from werkzeug.utils import secure_filename
from flask_cors import CORS

# Add dashboard to path
dashboard_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(dashboard_dir))

from task_monitor import TaskMonitor
from chat_session import ChatSessionStore
from chat_context import build_base_snapshot, get_deep_task_context, extract_task_id
from agent_chat import call_chat_with_tools, stream_chat_with_tools
from ollama_api_logger import OllamaAPILogger
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


def _json_error_envelope(view):
    """Map common exception types to proper HTTP status codes.

    ValueError → 400 (bad client input).
    FileNotFoundError → 404 (missing resource — note: subclass of OSError, so
      it must be listed before any OSError arm if one is added later).
    requests.ConnectionError → 503 (upstream dependency down, e.g. RAG API).
    Everything else → 500.

    All branches preserve ``str(e)`` in the response body. A redaction step
    (return a UUID to the client + log the full error server-side) is a known
    follow-up that is intentionally deferred; for now the priority is returning
    the correct status codes.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except _requests.exceptions.ConnectionError as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return wrapper

# Initialize task monitor
monitor = TaskMonitor(PROJECT_ROOT)

# Initialize chat components
chat_session_store = ChatSessionStore(max_history_turns=20)

# Initialize Ollama API logger
ollama_api_logger = OllamaAPILogger(PROJECT_ROOT / "logs" / "dashboard")

# Load chat system prompt template
chat_system_prompt_path = dashboard_dir / "chat_system_prompt.md"
CHAT_SYSTEM_PROMPT_TEMPLATE = ""
if chat_system_prompt_path.exists():
    CHAT_SYSTEM_PROMPT_TEMPLATE = chat_system_prompt_path.read_text(encoding='utf-8')

# Defaults used both as the inner fallback (when config.json omits a key)
# and as the outer fallback (when load_config itself blows up). Hoisted to
# module-top so the two paths can't drift.
DEFAULT_CHAT_MODEL = "qwen3.5:9b"
DEFAULT_CHAT_TIMEOUT_S = 240
DEFAULT_CHAT_MAX_TOOL_TURNS = 8
DEFAULT_RAG_BASE_URL = "http://localhost:8000"
DEFAULT_CHAT_OPTIONS_STANDARD = {
    "temperature": 0.7, "top_p": 0.8, "top_k": 20,
    "presence_penalty": 1.5, "repeat_penalty": 1.0, "num_ctx": 32768,
}
DEFAULT_CHAT_OPTIONS_THINKING = {
    "temperature": 1.0, "top_p": 0.95, "top_k": 20,
    "presence_penalty": 1.5, "repeat_penalty": 1.0, "num_ctx": 32768,
}

# Load config for chat settings
CHAT_MODELS = {}
CHAT_MODEL = DEFAULT_CHAT_MODEL
CHAT_TIMEOUT = DEFAULT_CHAT_TIMEOUT_S
CHAT_MAX_TOOL_TURNS = DEFAULT_CHAT_MAX_TOOL_TURNS
CHAT_OPTIONS_STANDARD = DEFAULT_CHAT_OPTIONS_STANDARD
CHAT_OPTIONS_THINKING = DEFAULT_CHAT_OPTIONS_THINKING

try:
    config = _load_config()
    chat_config = config._config.get("chat", {})
    CHAT_TIMEOUT = chat_config.get("timeout", DEFAULT_CHAT_TIMEOUT_S)
    CHAT_MAX_TOOL_TURNS = chat_config.get("max_tool_turns", DEFAULT_CHAT_MAX_TOOL_TURNS)

    # Parse models array (new structure) with fallback to old single model
    models_list = chat_config.get("models", [])
    if models_list:
        # New structure: chat.models is an array
        for model_config in models_list:
            model_name = model_config.get("name", "")
            if model_name:
                CHAT_MODELS[model_name] = {
                    "label": model_config.get("label", model_name),
                    "is_default": model_config.get("is_default", False),
                    "options_standard": model_config.get("options_standard", DEFAULT_CHAT_OPTIONS_STANDARD),
                    "options_thinking": model_config.get("options_thinking", DEFAULT_CHAT_OPTIONS_THINKING),
                }
                if model_config.get("is_default"):
                    CHAT_MODEL = model_name
    else:
        # Backward compatibility: old structure with single chat.model
        old_model = chat_config.get("model")
        if old_model:
            CHAT_MODEL = old_model
            CHAT_MODELS[old_model] = {
                "label": old_model,
                "is_default": True,
                "options_standard": chat_config.get("options_standard", DEFAULT_CHAT_OPTIONS_STANDARD),
                "options_thinking": chat_config.get("options_thinking", DEFAULT_CHAT_OPTIONS_THINKING),
            }
        else:
            # No models defined, use defaults
            CHAT_MODELS[DEFAULT_CHAT_MODEL] = {
                "label": DEFAULT_CHAT_MODEL,
                "is_default": True,
                "options_standard": DEFAULT_CHAT_OPTIONS_STANDARD,
                "options_thinking": DEFAULT_CHAT_OPTIONS_THINKING,
            }
            CHAT_MODEL = DEFAULT_CHAT_MODEL

    # Set current chat options based on the default model
    if CHAT_MODEL in CHAT_MODELS:
        CHAT_OPTIONS_STANDARD = CHAT_MODELS[CHAT_MODEL]["options_standard"]
        CHAT_OPTIONS_THINKING = CHAT_MODELS[CHAT_MODEL]["options_thinking"]
except Exception:
    CHAT_MODELS[DEFAULT_CHAT_MODEL] = {
        "label": DEFAULT_CHAT_MODEL,
        "is_default": True,
        "options_standard": DEFAULT_CHAT_OPTIONS_STANDARD,
        "options_thinking": DEFAULT_CHAT_OPTIONS_THINKING,
    }
    CHAT_MODEL = DEFAULT_CHAT_MODEL


def _get_model_config(model_name: str) -> dict:
    """Get options for a given model name, with fallback to defaults."""
    if model_name in CHAT_MODELS:
        return {
            "options_standard": CHAT_MODELS[model_name]["options_standard"],
            "options_thinking": CHAT_MODELS[model_name]["options_thinking"],
        }
    # Fallback to defaults if model not found
    return {
        "options_standard": DEFAULT_CHAT_OPTIONS_STANDARD,
        "options_thinking": DEFAULT_CHAT_OPTIONS_THINKING,
    }


@app.route("/")
def index():
    """Serve dashboard UI with the per-process token embedded as a meta tag."""
    return render_template("index.html", dashboard_token=DASHBOARD_TOKEN)


@app.route("/api/models", methods=["GET"])
@_json_error_envelope
def get_models():
    """Get available chat models."""
    models_list = [
        {
            "name": name,
            "label": config["label"],
            "is_default": config["is_default"],
        }
        for name, config in CHAT_MODELS.items()
    ]
    return jsonify({"models": models_list}), 200


@app.route("/api/status")
@_json_error_envelope
def get_status():
    """Get system status and metrics."""
    status = monitor.get_system_status()
    return jsonify(status), 200


@app.route("/api/tasks")
@_json_error_envelope
def get_tasks():
    """Get all tasks with optional filtering."""
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


@app.route("/api/tasks/<task_id>")
@_json_error_envelope
def get_task_detail(task_id):
    """Get complete task details including result and logs."""
    task = monitor.get_task_detail(task_id)
    if not task:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    return jsonify(task), 200


@app.route("/api/tasks/<task_id>/payload")
@_json_error_envelope
def get_task_payload(task_id):
    """Get raw task file content."""
    payload = monitor.get_task_payload(task_id)
    if not payload:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    return jsonify({"id": task_id, "content": payload}), 200


@app.route("/api/agents")
@_json_error_envelope
def get_agents():
    """Get per-agent statistics."""
    stats = monitor.get_agent_stats()
    return jsonify(stats), 200


@app.route("/api/agents/<agent>/logs")
@_json_error_envelope
def get_agent_logs(agent):
    """Get recent logs for a specific agent."""
    lines = request.args.get("lines", 50, type=int)
    logs = monitor.get_agent_logs(agent, lines=lines)
    return jsonify({"agent": agent, "logs": logs}), 200


@app.route("/api/pending-approvals")
@_json_error_envelope
def get_pending_approvals():
    """Get all tasks awaiting approval."""
    tasks = monitor.get_pending_approvals()
    return jsonify({"tasks": tasks, "count": len(tasks)}), 200


@app.route("/api/pending-approvals/<task_id>/approve", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def approve_task(task_id):
    """Approve a pending task."""
    success = monitor.approve_task(task_id)
    if not success:
        return jsonify({"error": f"Task {task_id} not found"}), 404
    return jsonify({"status": "approved", "task_id": task_id}), 200


@app.route("/api/pending-approvals/<task_id>/reject", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def reject_task(task_id):
    """Reject a pending task."""
    body = request.get_json() or {}
    reason = body.get("reason", "Rejected by user")
    success = monitor.reject_task(task_id, reason)
    if not success:
        return jsonify({"error": f"Task {task_id} not found"}), 404
    return jsonify({"status": "rejected", "task_id": task_id}), 200


@app.route("/api/results/<agent>")
@_json_error_envelope
def get_results(agent):
    """Get completed and failed results for a specific agent."""
    results = monitor.get_results_by_agent(agent)
    return jsonify(results), 200


@app.route("/api/tasks/completed", methods=["GET"])
@_json_error_envelope
def get_completed_tasks():
    """Return completed parent tasks available as context files."""
    tasks = monitor.get_completed_parent_tasks(limit=100)
    return jsonify(tasks), 200


@app.route("/api/tasks/submit", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def submit_task():
    """Submit a new task to the orchestrator."""
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

    # Create task file. If create_task_file raises ValueError (e.g. a
    # path-traversal attempt in context_files — see safe_read_context
    # hardening in task_io.py), the envelope decorator converts it to a 400.
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
@_json_error_envelope
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
@_json_error_envelope
def rag_list_documents():
    """List all documents in the knowledge base.

    ConnectionError → 503 mapping is provided by the envelope decorator.
    """
    resp = _requests.get(f"{_rag_base_url()}/documents", timeout=10)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/rag/ingest", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def rag_ingest():
    """Ingest a document into the knowledge base."""
    body = request.get_json() or {}
    resp = _requests.post(f"{_rag_base_url()}/ingest", json=body, timeout=600)
    return jsonify(resp.json()), resp.status_code


@app.route("/api/rag/documents/<doc_id>", methods=["DELETE"])
@require_dashboard_token
@_json_error_envelope
def rag_delete_document(doc_id):
    """Delete a document from the knowledge base."""
    resp = _requests.delete(f"{_rag_base_url()}/documents/{doc_id}", timeout=10)
    return jsonify(resp.json()), resp.status_code


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
# Ollama API Logging
# ---------------------------------------------------------------------------

@app.route("/api/ollama/logs", methods=["GET"])
@_json_error_envelope
def get_ollama_logs():
    """Get Ollama API logs, optionally filtered by session."""
    limit = request.args.get("limit", 100, type=int)
    session_id = request.args.get("session_id", None)
    logs = ollama_api_logger.read_logs(limit=limit, session_id=session_id)
    return jsonify({"logs": logs, "count": len(logs)}), 200


@app.route("/api/ollama/logs/<session_id>", methods=["GET"])
@_json_error_envelope
def get_ollama_logs_for_session(session_id):
    """Get Ollama API logs for a specific session."""
    limit = request.args.get("limit", 100, type=int)
    logs = ollama_api_logger.read_logs(limit=limit, session_id=session_id)
    return jsonify({"session_id": session_id, "logs": logs, "count": len(logs)}), 200


@app.route("/api/ollama/logs/clear", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def clear_ollama_logs():
    """Clear all Ollama API logs."""
    ollama_api_logger.clear_logs()
    return jsonify({"status": "cleared"}), 200


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def chat():
    """Chat endpoint with LLM and tools."""
    body = request.get_json() or {}
    user_message = body.get("message", "").strip()
    session_id = body.get("session_id")
    client_timestamp = body.get("timestamp", "").strip()  # ISO string from browser
    thinking_mode = bool(body.get("thinking_mode", False))
    requested_model = body.get("model", CHAT_MODEL).strip()

    if not user_message:
        return jsonify({"error": "message is required"}), 400

    # Validate and use requested model, fallback to default if invalid
    selected_model = requested_model if requested_model in CHAT_MODELS else CHAT_MODEL

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

    # Get model-specific options
    model_config = _get_model_config(selected_model)
    chat_options = model_config["options_thinking"] if thinking_mode else model_config["options_standard"]

    # Call LLM with tools — pass the timestamped message so the model sees timing.
    # OllamaError is mapped explicitly to 503 (upstream LLM unavailable) before
    # the envelope decorator would otherwise turn it into a 500.
    try:
        reply = call_chat_with_tools(
            model=selected_model,
            system_prompt=system_prompt,
            history=history,
            user_message=stamped_user_message,
            max_tool_turns=CHAT_MAX_TOOL_TURNS,
            options=chat_options,
            think=thinking_mode,
            session_id=session_id,
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


@app.route("/api/chat/stream", methods=["POST"])
@require_dashboard_token
def chat_stream():
    """SSE streaming chat endpoint — yields newline-delimited JSON events.

    Event types (all as ``data: {json}\\n\\n`` lines):
      {"type": "meta",      "session_id": str}
      {"type": "tool_call", "name": str, "args": dict}
      {"type": "thinking",  "text": str}
      {"type": "token",     "text": str}
      {"type": "done",      "full_content": str, "action"?: dict}
      {"type": "error",     "message": str}

    Intentionally NOT wrapped in @_json_error_envelope — a streaming
    response must be a plain Response, not a jsonify'd envelope.
    """
    import json as _json

    body = request.get_json() or {}
    user_message = body.get("message", "").strip()
    session_id = body.get("session_id")
    client_timestamp = body.get("timestamp", "").strip()
    thinking_mode = bool(body.get("thinking_mode", False))
    requested_model = body.get("model", CHAT_MODEL).strip()

    if not user_message:
        return jsonify({"error": "message is required"}), 400

    # Validate and use requested model, fallback to default if invalid
    selected_model = requested_model if requested_model in CHAT_MODELS else CHAT_MODEL

    if not session_id:
        session_id = chat_session_store.new_session()

    history = chat_session_store.get_history(session_id)

    base_snapshot = build_base_snapshot(PROJECT_ROOT)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    system_prompt = (
        CHAT_SYSTEM_PROMPT_TEMPLATE
        .replace("{PIPELINE_SNAPSHOT}", base_snapshot)
        .replace("{NOW}", now_str)
    )

    ts_label = client_timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stamped_user_message = f"[{ts_label}] {user_message}"

    task_id_ref = extract_task_id(user_message)
    if task_id_ref:
        deep_context = get_deep_task_context(task_id_ref, PROJECT_ROOT)
        system_prompt = system_prompt.replace("{PIPELINE_SNAPSHOT}", f"{base_snapshot}{deep_context}")

    # Get model-specific options
    model_config = _get_model_config(selected_model)
    chat_options = model_config["options_thinking"] if thinking_mode else model_config["options_standard"]

    def generate():
        # First event: session metadata so the client can store the session id
        yield f"data: {_json.dumps({'type': 'meta', 'session_id': session_id})}\n\n"

        full_content = ""
        for event in stream_chat_with_tools(
            model=selected_model,
            system_prompt=system_prompt,
            history=history,
            user_message=stamped_user_message,
            max_tool_turns=CHAT_MAX_TOOL_TURNS,
            options=chat_options,
            think=thinking_mode,
            session_id=session_id,
        ):
            if event["type"] == "done":
                full_content = event.get("full_content", "")

                # Handle CREATE_TASK block
                action = None
                task_match = re.search(
                    r'<CREATE_TASK>\s*(\{[^}]+\})\s*</CREATE_TASK>',
                    full_content, re.DOTALL
                )
                if task_match:
                    try:
                        task_data = _json.loads(task_match.group(1))
                        t_type = task_data.get("type", "code").strip()
                        t_priority = task_data.get("priority", "medium").strip()
                        t_desc = task_data.get("description", "").strip()
                        t_expected = task_data.get("expected_output", "See task description.").strip()
                        if t_desc:
                            inbox_path = PROJECT_ROOT / "inbox"
                            t_path = create_task_file(
                                inbox_path=inbox_path,
                                task_type=t_type,
                                description=t_desc,
                                expected_output=t_expected,
                                priority=t_priority,
                                created_by="chat",
                                assigned_to="orchestrator",
                                context_files=[],
                            )
                            new_task_id = t_path.stem.replace(".task", "")
                            action = {"type": "task_created", "task_id": new_task_id}
                        full_content = re.sub(
                            r'<CREATE_TASK>.*?</CREATE_TASK>', '',
                            full_content, flags=re.DOTALL
                        ).strip()
                    except Exception:
                        pass

                # Persist to session history
                chat_session_store.append(session_id, "user", stamped_user_message)
                chat_session_store.append(session_id, "assistant", full_content)

                done_payload = {"type": "done", "full_content": full_content}
                if action:
                    done_payload["action"] = action
                yield f"data: {_json.dumps(done_payload)}\n\n"
            else:
                yield f"data: {_json.dumps(event)}\n\n"

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/chat/clear", methods=["POST"])
@require_dashboard_token
@_json_error_envelope
def clear_chat():
    """Clear chat history for a session."""
    body = request.get_json() or {}
    session_id = body.get("session_id")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    chat_session_store.clear(session_id)
    return jsonify({"status": "cleared"}), 200


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
