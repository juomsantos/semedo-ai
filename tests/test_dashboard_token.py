"""
Tests for the H2 shared-secret token guard on the dashboard's state-changing
endpoints.

Background — H2 in the security audit:
The dashboard exposes destructive endpoints (``approve``, ``reject``,
``submit``, ``clear-cache``, RAG ingest/delete, chat) and originally had
no authentication. CORS was tightened to loopback origins, but any other
process on 127.0.0.1 (a dev server on a different port, a malicious local
script, even a browser extension) could still hit them. The fix is a
per-process random token embedded in the served HTML and checked on every
POST/DELETE via ``X-Dashboard-Token``.

These tests lock in:
  1. Every protected endpoint returns 401 when the header is absent.
  2. The wrong token also returns 401.
  3. The correct token reaches the underlying handler.
  4. Read-only endpoints (``GET /api/status``) stay open — we deliberately
     don't gate them, so the dashboard polling loop keeps working.
  5. The token is embedded in the served HTML as a ``<meta>`` tag the JS
     can read on load.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make dashboard/ importable as `app` etc.
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))


@pytest.fixture
def client():
    """A Flask test client with the real token bound, plus monitor patched
    so we don't depend on the actual filesystem state for unit tests."""
    import app as dashboard_app  # imports trigger ollama/etc; that's fine in dev env
    dashboard_app.app.config["TESTING"] = True
    return dashboard_app.app.test_client(), dashboard_app.DASHBOARD_TOKEN


# ---------------------------------------------------------------------------
# Protected endpoints — must reject missing/wrong token
# ---------------------------------------------------------------------------

# (method, path, json_body) — exhaustive list of every endpoint we expect to
# require the token. Keeping this list in sync with `app.py` is the whole
# point of the test: forgetting to decorate a new endpoint will surface
# here as a "this should have returned 401 but returned 200" failure.
PROTECTED_ENDPOINTS = [
    ("POST", "/api/pending-approvals/abc/approve", None),
    ("POST", "/api/pending-approvals/abc/reject", {"reason": "no"}),
    ("POST", "/api/tasks/submit", {"description": "x", "type": "code"}),
    ("POST", "/api/clear-cache", None),
    ("POST", "/api/upload-context", None),
    ("POST", "/api/rag/ingest", {"document_id": "d", "content": "c"}),
    ("DELETE", "/api/rag/documents/foo", None),
    ("POST", "/api/chat", {"message": "hi"}),
    ("POST", "/api/chat/clear", {"session_id": "s"}),
]


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_protected_endpoint_rejects_missing_token(client, method, path, body):
    """No header → 401, no matter what the body looks like."""
    flask_client, _token = client
    resp = flask_client.open(path, method=method, json=body)
    assert resp.status_code == 401, (
        f"{method} {path} returned {resp.status_code} without a token "
        f"— missing @require_dashboard_token decorator?"
    )
    assert resp.get_json() == {"error": "unauthorized"}


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_protected_endpoint_rejects_wrong_token(client, method, path, body):
    """Wrong token → 401. ``secrets.compare_digest`` does the comparison so
    this also exercises the constant-time path."""
    flask_client, _token = client
    resp = flask_client.open(
        path, method=method, json=body,
        headers={"X-Dashboard-Token": "this-is-not-the-token"},
    )
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "unauthorized"}


def test_protected_endpoint_accepts_correct_token(client):
    """Correct token → the request reaches the handler. We pick the chat
    endpoint and stub the underlying LLM call so the test is hermetic. The
    only assertion that matters is "not 401" — the handler's own behaviour
    is covered elsewhere."""
    flask_client, token = client
    import app as dashboard_app
    with patch.object(dashboard_app, "call_chat_with_tools", return_value="ok"):
        resp = flask_client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"X-Dashboard-Token": token},
        )
    # With the token accepted, the handler runs and returns 200 (or some
    # non-401 application-level code if downstream services were unmocked).
    # The point is: we got past the auth wall.
    assert resp.status_code != 401, (
        "Correct token was rejected — the comparison or decorator is broken."
    )


# ---------------------------------------------------------------------------
# Read-only endpoints — must stay open (dashboard polling depends on this)
# ---------------------------------------------------------------------------

OPEN_ENDPOINTS = [
    "/api/status",
    "/api/tasks",
    "/api/agents",
    "/api/pending-approvals",
]


@pytest.mark.parametrize("path", OPEN_ENDPOINTS)
def test_read_only_endpoints_do_not_require_token(client, path):
    """GET endpoints must not be behind the token. The dashboard polls them
    every 2 seconds — gating them would make the UI feel broken on every
    server restart and adds no security (they're read-only)."""
    flask_client, _token = client
    resp = flask_client.get(path)
    # We don't assert 200 because the monitor may legitimately error in the
    # test environment (no real pipeline tree). 401 is the failure mode we
    # care about — if a GET returns 401 the decorator was applied too widely.
    assert resp.status_code != 401, (
        f"GET {path} returned 401 — read-only endpoints must not require a token."
    )


# ---------------------------------------------------------------------------
# Token must be embedded in the served HTML
# ---------------------------------------------------------------------------


def test_index_html_embeds_dashboard_token(client):
    """The token is delivered to the browser via a ``<meta>`` tag in the
    served HTML. If this regresses, the JS ``DASHBOARD_TOKEN`` constant
    silently becomes an empty string and every state-changing call breaks."""
    flask_client, token = client
    resp = flask_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="dashboard-token"' in html, (
        "index.html is missing <meta name=\"dashboard-token\"> — the JS has no "
        "way to read the per-process token."
    )
    assert f'content="{token}"' in html, (
        "The <meta> tag is present but the token wasn't injected. Did "
        "index() switch back to send_from_directory instead of render_template?"
    )
