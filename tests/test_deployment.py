import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import DatabaseError, connections
from django.test import override_settings
from django.urls import reverse

from financial_planner.healthcheck import probe_host
from financial_planner.settings import allowed_hosts_from_env


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
def test_health_probe_succeeds_when_allowed_hosts_env_has_leading_space(client, monkeypatch):
    # A wrapped env file can leave a space after DJANGO_ALLOWED_HOSTS=. The
    # probe already strips it; Django's ALLOWED_HOSTS must too, or validate_host
    # answers 400 and the container is unhealthy while the app is reachable.
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", " basement-pc.example-tailnet.ts.net")
    raw = os.environ["DJANGO_ALLOWED_HOSTS"]
    hosts = allowed_hosts_from_env(raw)
    probed = probe_host(raw)

    assert hosts == [probed] == ["basement-pc.example-tailnet.ts.net"]
    with override_settings(ALLOWED_HOSTS=hosts):
        response = client.get(reverse("health"), HTTP_HOST=probed, HTTP_X_FORWARDED_PROTO="https")

    assert response.status_code == 200


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


def test_container_scripts_and_dockerfiles_use_lf_line_endings():
    # A CR before the newline in a "#!/bin/sh" line makes the kernel look for
    # an interpreter named "/bin/sh\r", so the container cannot start. Git on
    # Windows (core.autocrlf=true) adds the CR unless .gitattributes forbids it.
    root = Path(__file__).resolve().parent.parent
    files = [*root.glob("scripts/*.sh"), *root.glob("ops/**/*.sh"), root / "Dockerfile", root / "ops/backup/Dockerfile"]

    assert files
    assert [str(path.relative_to(root)) for path in files if b"\r" in path.read_bytes()] == []


def test_gitattributes_forces_lf_for_container_files():
    attributes = (Path(__file__).resolve().parent.parent / ".gitattributes").read_text()

    assert "*.sh text eol=lf" in attributes
    assert "Dockerfile text eol=lf" in attributes


def test_gunicorn_access_log_never_records_query_strings_or_referrers():
    # Transaction search text travels in the query string (/transactions/?q=...)
    # and the Referer header repeats it on the next page, so neither may reach
    # the Docker logs. %(U)s is the path alone; %(r)s, %(q)s, and %(f)s are not.
    script = (Path(__file__).resolve().parent.parent / "scripts/start-production.sh").read_text()
    log_format = next(line for line in script.splitlines() if "--access-logformat" in line)

    assert "%(U)s" in log_format
    assert [token for token in ("%(r)s", "%(q)s", "%(f)s", "%({referer}i)s") if token in log_format] == []
