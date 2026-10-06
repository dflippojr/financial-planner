"""In-process fake Anthropic Messages and OpenAI Responses servers. Synthetic only."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANTHROPIC_KEY = "sk-ant-synthetic-canary-key-0001"
OPENAI_KEY = "sk-synthetic-canary-key-0002"


class FakeProviderState:
    def __init__(self):
        self.requests = []
        # Each entry is (status, payload); when empty a plain text answer is returned.
        self.script = []
        self.answer = "Synthetic answer."

    def next_reply(self, path):
        if self.script:
            return self.script.pop(0)
        if path == "/v1/messages":
            return 200, anthropic_text(self.answer)
        return 200, openai_text(self.answer)


def anthropic_text(text, tokens=(11, 7)):
    return {
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


def anthropic_tool(name, args, call_id="toolu_1", tokens=(20, 5)):
    return {
        "type": "message",
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": args}],
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


def openai_text(text, tokens=(13, 9)):
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


def openai_tool(name, args, call_id="call_1", tokens=(21, 4)):
    return {
        "output": [{"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}],
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


def start_fake_provider():
    state = FakeProviderState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            status, payload = state.next_reply(self.path)
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state, f"http://127.0.0.1:{server.server_address[1]}", server
