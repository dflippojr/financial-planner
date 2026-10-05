"""Agent Harness connect errors (#212). Synthetic tokens and a fake harness only."""

import io
import logging
import traceback
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth
from tests.test_ai_provider import TOKEN, make_member

from finance.ai_services import (
    HARNESS_NOT_FOUND,
    HARNESS_UNREACHABLE,
    TOKEN_FORMAT,
    TOKEN_REJECTED,
    AiError,
    connect_harness,
    connection_for,
)
from finance.ai_urls import HarnessUrlError, parse_harness_url
from finance.log_redaction import redact


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


def _signed_in_client(user, *, fresh=True):
    client = Client(raise_request_exception=False)
    client.force_login(user)
    if fresh:
        stamp_recent_auth(client)
    return client


def _connect_message(client, base_url, token=TOKEN):
    response = client.post(reverse("ai-connect"), {"base_url": base_url, "token": token}, follow=True)
    assert response.status_code == 200
    return [str(message) for message in response.context["messages"]]


@pytest.mark.parametrize("suffix", ["/api/v1", "/api/v1/"])
def test_parse_drops_trailing_api_path(suffix):
    assert parse_harness_url(f"https://tower.example.ts.net{suffix}") == "https://tower.example.ts.net"
    assert parse_harness_url(f"http://localhost:8000/harness{suffix}") == "http://localhost:8000/harness"


@pytest.mark.django_db
def test_url_ending_in_api_path_connects_like_the_bare_url(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=f"{url}/api/v1/", token=TOKEN)
    assert connection_for(person).base_url == url
    assert ("GET", "/api/v1") in state.requests
    assert not any(path.startswith("/api/v1/api/v1") for _method, path in state.requests)


@pytest.mark.django_db
def test_rejected_token_names_the_harness_settings(harness):
    state, url = harness
    state.token = "ha-a-different-synthetic-token"
    user, _person, _household = make_member("owner")
    messages = _connect_message(_signed_in_client(user), url)
    assert messages == [TOKEN_REJECTED]
    assert "Settings → Apps" in TOKEN_REJECTED


@pytest.mark.django_db
def test_wrong_path_says_the_url_is_not_a_harness_api(harness):
    _state, url = harness
    user, _person, _household = make_member("owner")
    messages = _connect_message(_signed_in_client(user), f"{url}/not-the-harness")
    assert messages == [HARNESS_NOT_FOUND]


@pytest.mark.django_db
def test_unreachable_harness_explains_localhost_in_docker():
    user, _person, _household = make_member("owner")
    messages = _connect_message(_signed_in_client(user), "http://127.0.0.1:1")
    assert messages == [HARNESS_UNREACHABLE]
    assert "tailnet" in HARNESS_UNREACHABLE


def test_the_three_failures_have_distinct_messages():
    assert len({TOKEN_REJECTED, HARNESS_NOT_FOUND, HARNESS_UNREACHABLE}) == 3


@pytest.mark.parametrize(
    "raw",
    [
        "http://[::1",
        "http://localhost:abc",
        "http://localhost:0",
        "http://localhost:99999",
        "https://bad host.ts.net",
        "http://localhost/ space",
        "http://localhost/café",
        "https://tést.ts.net",
    ],
)
def test_malformed_urls_are_refused_with_a_message(raw):
    with pytest.raises(HarnessUrlError):
        parse_harness_url(raw)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "raw",
    ["http://[::1", "http://localhost:abc", "http://localhost/ space", "http://localhost/café"],
)
def test_malformed_url_post_is_a_message_not_a_500(raw):
    user, person, _household = make_member("owner")
    messages = _connect_message(_signed_in_client(user), raw)
    assert len(messages) == 1
    assert connection_for(person) is None


@pytest.mark.django_db
@pytest.mark.parametrize("token", ["ha-synthetic’token", "ha-synthetic\ntoken", "ha-synthetic token"])
def test_badly_pasted_token_is_a_message_not_a_500(harness, token):
    state, url = harness
    user, person, _household = make_member("owner")
    messages = _connect_message(_signed_in_client(user), url, token)
    assert messages == [TOKEN_FORMAT]
    assert connection_for(person) is None
    assert state.requests == []


@pytest.mark.django_db
def test_header_the_client_refuses_is_reported_as_unreachable(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    with patch("finance.ai_services._looks_like_app_token", return_value=True):
        with pytest.raises(AiError) as caught:
            connect_harness(person, base_url=url, token="ha-synthetic\ntoken")
    assert str(caught.value) == HARNESS_UNREACHABLE
    printed = "".join(traceback.format_exception(caught.value))
    assert "Bearer" not in printed
    assert "Invalid header" not in printed


@pytest.mark.django_db
def test_expired_reauth_window_redirects_to_reauth(harness):
    state, url = harness
    user, person, _household = make_member("owner")
    client = _signed_in_client(user, fresh=False)
    response = client.post(reverse("ai-connect"), {"base_url": url, "token": TOKEN})
    assert response.status_code == 302
    assert response.url.startswith(reverse("reauth"))
    assert client.get(response.url).status_code == 200
    assert connection_for(person) is None
    assert state.requests == []


def _console_handler():
    handlers = [h for h in logging.getLogger("django.request").handlers if isinstance(h, logging.StreamHandler)]
    assert handlers, "settings.LOGGING must attach the console handler to django.request"
    return handlers[0]


@pytest.mark.django_db
def test_view_exception_logs_a_traceback_without_the_token(settings):
    assert settings.DEBUG is False
    assert settings.LOGGING["handlers"]["console"]["stream"] == "ext://sys.stdout"
    user, _person, _household = make_member("owner")
    client = _signed_in_client(user)
    secret = "ha-synthetic-secret-in-a-traceback"
    handler = _console_handler()
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        with patch("finance.ai_views.connect_harness", side_effect=RuntimeError(f"Bearer {secret} broke")):
            response = client.post(
                reverse("ai-connect"),
                {"base_url": "http://localhost:1", "token": secret},
            )
    finally:
        handler.setStream(previous)
    assert response.status_code == 500
    logged = buffer.getvalue()
    assert "Internal Server Error: /settings/ai/connect/" in logged
    assert "Traceback (most recent call last)" in logged
    assert "RuntimeError" in logged
    assert secret not in logged
    assert "synthetic-secret" not in logged


def test_redact_strips_tokens_and_url_credentials():
    text = redact("b'Bearer ha-abc123' sha-256 https://user:pa55@bridge.example/simplefin")
    assert "ha-abc123" not in text
    assert "pa55" not in text
    assert "sha-256" in text
