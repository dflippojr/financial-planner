"""Operator allow-listed Agent Harness origins (#409). Synthetic servers and tokens only."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth
from tests.test_ai_provider import TOKEN, make_member

from finance import ai_http
from finance.ai_harness import NotAHarness, list_backends, model_status
from finance.ai_http import HarnessHttpError, json_request
from finance.ai_services import HARNESS_UNAVAILABLE, connect_harness, connection_for
from finance.ai_urls import HARNESS_DISABLED, ORIGIN_NOT_ALLOWED, HarnessUrlError, parse_harness_url


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def other_service(settings):
    """An allow-listed endpoint that is not a harness; each test sets its reply."""
    reply = {"status": 200, "body": b"{}", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _answer(self):
            reply["requests"].append((self.command, self.path))
            reply.setdefault("authorization", []).append(self.headers.get("Authorization"))
            self.send_response(reply["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply["body"])))
            self.end_headers()
            self.wfile.write(reply["body"])

        do_GET = do_POST = do_DELETE = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    host, port = server.server_address
    url = f"http://{host}:{port}"
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = [url]
    try:
        yield reply, url
    finally:
        server.shutdown()
        server.server_close()


def _connect_messages(user, base_url):
    client = Client(raise_request_exception=False)
    client.force_login(user)
    stamp_recent_auth(client)
    response = client.post(reverse("ai-connect"), {"base_url": base_url, "token": TOKEN}, follow=True)
    assert response.status_code == 200
    return [str(message) for message in response.context["messages"]]


def test_the_default_allows_only_the_harness_on_this_host():
    assert parse_harness_url("http://host.docker.internal:8100") == "http://host.docker.internal:8100"
    for raw in (
        "http://host.docker.internal:8101",
        "http://host.docker.internal:22",
        "https://host.docker.internal:8100",
        "http://127.0.0.1:8100",
        "http://localhost:8100",
        "https://tower.example.ts.net",
    ):
        with pytest.raises(HarnessUrlError) as caught:
            parse_harness_url(raw)
        assert str(caught.value) == ORIGIN_NOT_ALLOWED


def test_default_ports_and_host_case_match_the_allowed_origin(settings):
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = ["https://Tower.Example.ts.net", "http://harness.example:80"]
    assert parse_harness_url("https://tower.example.ts.net:443/") == "https://tower.example.ts.net:443"
    assert parse_harness_url("http://harness.example") == "http://harness.example"


def test_a_path_prefix_is_refused(settings):
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = ["https://tower.example.ts.net"]
    with pytest.raises(HarnessUrlError):
        parse_harness_url("https://tower.example.ts.net/other-app")


@pytest.mark.parametrize("entry", ["tower.example.ts.net", "ftp://tower.example", "https://tower.example/app", "https://u:p@tower.example"])
def test_a_bad_allow_list_entry_is_a_configuration_error(settings, entry):
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = [entry]
    with pytest.raises(ImproperlyConfigured):
        parse_harness_url("https://tower.example")


@pytest.mark.django_db
def test_an_empty_allow_list_disables_the_harness(settings, harness):
    state, url = harness
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = []
    user, person, _household = make_member("owner")
    with pytest.raises(HarnessUrlError) as caught:
        parse_harness_url(url)
    assert str(caught.value) == HARNESS_DISABLED
    assert _connect_messages(user, url) == [HARNESS_DISABLED]
    assert connection_for(person) is None
    assert state.requests == []
    client = Client()
    client.force_login(user)
    page = client.get(reverse("settings-ai")).content.decode()
    assert "Agent Harness isn't enabled on this server." in page
    assert reverse("ai-connect") not in page


@pytest.mark.django_db
def test_requests_are_refused_once_the_origin_leaves_the_allow_list(settings, harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connection = connect_harness(person, base_url=url, token=TOKEN)
    seen = len(state.requests)
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = ["http://host.docker.internal:8100"]
    with pytest.raises(NotAHarness):
        list_backends(connection.base_url, TOKEN)
    with pytest.raises(NotAHarness):
        model_status(connection.base_url, TOKEN)
    assert len(state.requests) == seen


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, b'{"detail": "not found"}'),
        (401, b'{"detail": "unauthorized"}'),
        (403, b"<html>forbidden</html>"),
        (500, b"oops"),
        (200, b'{"service": "something else"}'),
        (200, b'["not", "a", "harness"]'),
        (200, b"<html>router login</html>"),
    ],
)
def test_every_failure_before_proof_gets_one_message(other_service, status, body):
    reply, url = other_service
    reply["status"], reply["body"] = status, body
    user, person, _household = make_member("owner")
    assert _connect_messages(user, url) == [HARNESS_UNAVAILABLE]
    assert connection_for(person) is None
    # Nothing beyond the discovery document is requested from a non-harness.
    assert reply["requests"] == [("GET", "/api/v1")]
    assert reply["authorization"] == [None]


@pytest.mark.django_db
def test_a_refused_port_gets_the_same_message(settings):
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = ["http://127.0.0.1:1"]
    user, _person, _household = make_member("owner")
    assert _connect_messages(user, "http://127.0.0.1:1") == [HARNESS_UNAVAILABLE]


def test_an_oversized_response_is_refused(other_service):
    reply, url = other_service
    reply["body"] = json.dumps({"api_version": "1", "pad": "x" * 2048}).encode()
    with patch.object(ai_http, "MAX_RESPONSE_BYTES", 1024):
        with pytest.raises(HarnessHttpError) as caught:
            json_request(f"{url}/api/v1", token=TOKEN)
    assert caught.value.status == 0
    assert json_request(f"{url}/api/v1", token=TOKEN)["api_version"] == "1"


def test_an_oversized_error_body_is_not_parsed(other_service):
    reply, url = other_service
    reply["status"] = 422
    reply["body"] = json.dumps({"error": {"code": "limit_reached"}, "pad": "x" * 2048}).encode()
    with patch.object(ai_http, "MAX_RESPONSE_BYTES", 1024):
        with pytest.raises(HarnessHttpError) as caught:
            json_request(f"{url}/api/v1", token=TOKEN)
    assert caught.value.status == 422
    assert caught.value.payload == {}
