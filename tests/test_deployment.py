from unittest.mock import patch

import pytest
from django.db import DatabaseError, connections
from django.test import override_settings
from django.urls import reverse

from financial_planner.healthcheck import probe_host


@pytest.mark.django_db
def test_health_is_public_and_reports_database_readiness(client):
    response = client.get(reverse("health"))

    assert response.status_code == 200
    assert response.content == b"ok\n"
    assert response.headers["Cache-Control"] == "max-age=0, no-cache, no-store, must-revalidate, private"


@pytest.mark.django_db
def test_health_failure_reveals_no_database_details(client):
    with patch.object(connections["default"], "cursor", side_effect=DatabaseError("secret detail")):
        response = client.get(reverse("health"))

    assert response.status_code == 503
    assert response.content == b"unavailable\n"
    assert b"secret" not in response.content


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
def test_tailscale_proxy_header_marks_request_secure(client):
    redirected = client.get(reverse("health"))
    proxied = client.get(reverse("health"), HTTP_X_FORWARDED_PROTO="https")

    assert redirected.status_code == 301
    assert redirected.headers["Location"].startswith("https://")
    assert proxied.status_code == 200


@pytest.mark.parametrize(
    ("allowed_hosts", "expected"),
    [
        ("basement-pc.example-tailnet.ts.net", "basement-pc.example-tailnet.ts.net"),
        ("basement-pc.example-tailnet.ts.net,localhost", "basement-pc.example-tailnet.ts.net"),
        (" .example.ts.net ", "example.ts.net"),
        ("*,real.example", "real.example"),
        ("", "localhost"),
        (",,*", "localhost"),
    ],
)
def test_health_probe_uses_a_host_the_deployment_allows(allowed_hosts, expected):
    assert probe_host(allowed_hosts) == expected


@pytest.mark.django_db
def test_health_probe_succeeds_when_only_the_tailnet_name_is_allowed(client):
    # The documented configuration allows only the MagicDNS name, so the old
    # probe's "Host: 127.0.0.1" was rejected with 400 and the container stayed
    # unhealthy even though the app and database were fine.
    allowed = "basement-pc.example-tailnet.ts.net"
    with override_settings(ALLOWED_HOSTS=[allowed]):
        rejected = client.get(reverse("health"), HTTP_HOST="127.0.0.1", HTTP_X_FORWARDED_PROTO="https")
        probed = client.get(reverse("health"), HTTP_HOST=probe_host(allowed), HTTP_X_FORWARDED_PROTO="https")

    assert rejected.status_code == 400
    assert probed.status_code == 200
