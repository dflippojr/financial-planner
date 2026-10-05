"""In-process fake Agent Harness Server for contract tests. Synthetic only."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class FakeHarnessState:
    def __init__(self):
        self.token = "ha-synthetic-app-token"
        self.revoked = False
        self.model_state = "sleeping"
        self.waking_seconds = 12
        self.requests = []
        self.session_failure = None
        self.sessions = {}
        self._next_id = 1
        self.need_tool = False
        self.tool_answered = False
        self.hosted_logged_in = True
        self.session_answer = None
        self.session_prompts = []
        self.running_polls = 0
        self.stuck_waiting_app = False
        self.session_get_errors = 0
        self.projects = [{"name": "financial-planner", "description": "", "target": "local"}]
        self.initial_session_status = "running"
        self.session_polls_left = {}
        self.session_creates = []
        self.session_messages = []
        self.session_contexts = []
        self.create_http_error = None
        self.warm_error = None
        self.followup_need_tool = False
        self.followup_answer = "synthetic-followup"
        self.pending_tool_calls = None
        self.tool_outputs = []
        self.session_create_delay = 0
        self.app_tools_only = {"local": True, "claude": True, "codex": False, "cursor": False}

    def backends(self):
        return [
            {
                "name": "local",
                "available": True,
                "logged_in": True,
                "auth": "local",
                "billing": "local",
                "model": "tower",
                "effort": "",
                "notice": "Available",
                "provider_policy": {"allowed": True, "available": True},
                "app_tools_only": self.app_tools_only.get("local", True),
            },
            {
                "name": "claude",
                "available": True,
                "logged_in": self.hosted_logged_in,
                "auth": "subscription",
                "billing": "subscription",
                "model": "default",
                "effort": "",
                "notice": "Available",
                "provider_policy": {"allowed": True, "available": True},
                "app_tools_only": self.app_tools_only.get("claude", True),
            },
            {
                "name": "codex",
                "available": False,
                "logged_in": False,
                "auth": "subscription",
                "billing": "subscription",
                "model": "",
                "effort": "",
                "notice": "Not logged in",
                "provider_policy": {"allowed": True, "available": False},
                "app_tools_only": self.app_tools_only.get("codex", False),
            },
            {
                "name": "cursor",
                "available": True,
                "logged_in": True,
                "auth": "subscription",
                "billing": "subscription",
                "model": "default",
                "effort": "",
                "notice": "Available",
                "provider_policy": {"allowed": True, "available": True},
                "app_tools_only": self.app_tools_only.get("cursor", False),
            },
        ]


def start_fake_harness(state=None):
    harness = state or FakeHarnessState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _auth(self):
            header = self.headers.get("Authorization", "")
            if harness.revoked or header != f"Bearer {harness.token}":
                self._json(401, {"detail": "unauthorized", "error": {"code": "provider_auth_required"}})
                return False
            return True

        def _json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            harness.requests.append(("GET", parsed.path))
            if parsed.path == "/api/v1":
                if not self._auth():
                    return
                self._json(
                    200,
                    {
                        "api_version": "1",
                        "projects": [],
                        "backends": harness.backends(),
                    },
                )
                return
            if parsed.path == "/api/v1/backends":
                if not self._auth():
                    return
                self._json(200, harness.backends())
                return
            if parsed.path == "/api/v1/projects":
                if not self._auth():
                    return
                self._json(200, harness.projects)
                return
            if parsed.path == "/api/v1/models/status":
                if not self._auth():
                    return
                self._json(
                    200,
                    [
                        {
                            "name": "tower",
                            "state": harness.model_state,
                            "waking_seconds": harness.waking_seconds,
                        }
                    ],
                )
                return
            if parsed.path.startswith("/api/v1/sessions/") and parsed.path.endswith("/tool_calls"):
                if not self._auth():
                    return
                session_id = parsed.path.split("/")[4]
                session = harness.sessions.get(session_id)
                if session is None:
                    self._json(404, {"detail": "not found"})
                    return
                pending = []
                if session.get("status") == "waiting_app" and not harness.tool_answered and not harness.stuck_waiting_app:
                    if harness.pending_tool_calls:
                        pending = harness.pending_tool_calls[0]
                    else:
                        pending = [
                            {
                                "call_id": "call-1",
                                "name": "list_accounts",
                                "args": {},
                            }
                        ]
                self._json(200, pending)
                return
            if parsed.path.startswith("/api/v1/sessions/"):
                if not self._auth():
                    return
                session_id = parsed.path.rsplit("/", 1)[-1]
                session = harness.sessions.get(session_id)
                if session is None:
                    self._json(404, {"detail": "not found"})
                    return
                if harness.session_get_errors > 0:
                    harness.session_get_errors -= 1
                    self._json(502, {"detail": "bad gateway"})
                    return
                left = harness.session_polls_left.get(session_id, 0)
                if left > 0:
                    harness.session_polls_left[session_id] = left - 1
                    if left == 1 and session.get("status") in {"running", "queued"}:
                        session["status"] = "done"
                        session["answer"] = "synthetic-ok"
                        session["prompt_tokens"] = 3
                        session["completion_tokens"] = 4
                self._json(200, session)
                return
            self._json(404, {"detail": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            harness.requests.append(("POST", parsed.path))
            body = self._read_json()
            if parsed.path == "/api/v1/models/warm":
                if not self._auth():
                    return
                if harness.warm_error:
                    self._json(
                        409,
                        {
                            "detail": "the local model cannot load",
                            "error": {"code": harness.warm_error, "message": "synthetic"},
                        },
                    )
                    return
                if harness.model_state in {"sleeping", "unloaded"}:
                    harness.model_state = "waking"
                self._json(200, {"name": "tower", "state": harness.model_state})
                return
            if parsed.path == "/api/v1/sessions":
                if not self._auth():
                    return
                harness.session_creates.append(body)
                harness.session_prompts.append(str(body.get("prompt") or ""))
                if harness.session_create_delay:
                    time.sleep(harness.session_create_delay)
                if harness.create_http_error:
                    error = harness.create_http_error
                    harness.create_http_error = None
                    self._json(
                        error.get("status", 400),
                        {
                            "detail": error.get("detail", "bad request"),
                            "error": {"code": error.get("code", "provider_error"), "message": "synthetic"},
                        },
                    )
                    return
                tools_only = bool(body.get("tools_only"))
                backend = str(body.get("backend") or "")
                if tools_only and "project" in body and body.get("project") not in (None, ""):
                    self._json(
                        400,
                        {
                            "detail": "tools_only sessions cannot include a project",
                            "error": {"code": "invalid_request", "message": "synthetic"},
                        },
                    )
                    return
                if tools_only and not harness.app_tools_only.get(backend, False):
                    self._json(
                        400,
                        {
                            "detail": "backend does not support tools_only",
                            "error": {"code": "app_tools_only_unsupported", "message": "synthetic"},
                        },
                    )
                    return
                session_id = f"ses-{harness._next_id}"
                harness._next_id += 1
                if harness.session_failure:
                    session = {
                        "id": session_id,
                        "status": "failed",
                        "failure": {"code": harness.session_failure, "message": "synthetic"},
                        "prompt_tokens": 1,
                        "completion_tokens": 0,
                    }
                    harness.session_failure = None
                elif harness.stuck_waiting_app:
                    session = {
                        "id": session_id,
                        "status": "waiting_app",
                        "prompt_tokens": 2,
                        "completion_tokens": 0,
                    }
                elif harness.need_tool and not harness.tool_answered:
                    session = {
                        "id": session_id,
                        "status": "waiting_app",
                        "prompt_tokens": 2,
                        "completion_tokens": 0,
                    }
                elif harness.running_polls:
                    session = {
                        "id": session_id,
                        "status": harness.initial_session_status,
                        "prompt_tokens": 3,
                        "completion_tokens": 0,
                    }
                    harness.session_polls_left[session_id] = harness.running_polls
                else:
                    session = {
                        "id": session_id,
                        "status": "done",
                        "answer": harness.session_answer if harness.session_answer is not None else "synthetic-ok",
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                    }
                harness.sessions[session_id] = session
                self._json(200, session)
                return
            if parsed.path.endswith("/messages") and parsed.path.startswith("/api/v1/sessions/"):
                if not self._auth():
                    return
                session_id = parsed.path.split("/")[4]
                session = harness.sessions.get(session_id)
                if session is None:
                    self._json(404, {"detail": "not found"})
                    return
                harness.session_messages.append(str(body.get("content") or ""))
                harness.tool_answered = False
                if harness.session_failure:
                    session["status"] = "failed"
                    session["failure"] = {"code": harness.session_failure, "message": "synthetic"}
                    harness.session_failure = None
                elif harness.followup_need_tool or harness.need_tool:
                    session["status"] = "waiting_app"
                else:
                    session["status"] = "done"
                    session["answer"] = harness.followup_answer
                    session["completion_tokens"] = 6
                self._json(200, session)
                return
            if parsed.path.endswith("/context") and parsed.path.startswith("/api/v1/sessions/"):
                if not self._auth():
                    return
                session_id = parsed.path.split("/")[4]
                if session_id not in harness.sessions:
                    self._json(404, {"detail": "not found"})
                    return
                harness.session_contexts.append(body.get("context") or [])
                self._json(200, {"ok": True})
                return
            if "/tool_calls/" in parsed.path:
                if not self._auth():
                    return
                session_id = parsed.path.split("/")[4]
                session = harness.sessions.get(session_id)
                if session is None:
                    self._json(404, {"detail": "not found"})
                    return
                harness.tool_answered = True
                harness.tool_outputs.append(str(body.get("output") or ""))
                if harness.pending_tool_calls:
                    harness.pending_tool_calls = harness.pending_tool_calls[1:]
                    if harness.pending_tool_calls:
                        session["status"] = "waiting_app"
                        harness.tool_answered = False
                    else:
                        session["status"] = "done"
                        session["answer"] = harness.session_answer if harness.session_answer is not None else f"tool:{body.get('output', '')[:80]}"
                        session["completion_tokens"] = 5
                else:
                    session["status"] = "done"
                    session["answer"] = f"tool:{body.get('output', '')[:80]}"
                    session["completion_tokens"] = 5
                self._json(200, {"ok": True})
                return
            self._json(404, {"detail": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return harness, f"http://{host}:{port}", server
