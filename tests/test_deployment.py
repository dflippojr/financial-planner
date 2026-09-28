from unittest.mock import patch

import pytest
from django.db import DatabaseError, connections
from django.test import override_settings
from django.urls import reverse


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
