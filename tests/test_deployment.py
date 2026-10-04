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
    assert "scripts/css_pins.env text eol=lf" in attributes
    assert "static/src/vendor/** text eol=lf" in attributes
    assert "static/vendor/** text eol=lf" in attributes


def test_gunicorn_access_log_never_records_query_strings_or_referrers():
    # Transaction search text travels in the query string (/transactions/?q=...)
    # and the Referer header repeats it on the next page, so neither may reach
    # the Docker logs. %(U)s is the path alone; %(r)s, %(q)s, and %(f)s are not.
    script = (Path(__file__).resolve().parent.parent / "scripts/start-production.sh").read_text()
    log_format = next(line for line in script.splitlines() if "--access-logformat" in line)

    assert "%(U)s" in log_format
    assert [token for token in ("%(r)s", "%(q)s", "%(f)s", "%({referer}i)s") if token in log_format] == []


def test_compose_stages_csv_uploads_on_a_memory_backed_mount():
    # An abandoned upload of a real bank export must never sit on disk, so the
    # staging directory has to live on a tmpfs and the app has to be pointed at it.
    compose = (Path(__file__).resolve().parent.parent / "compose.yml").read_text()

    assert "CSV_IMPORT_STAGING_DIR: /run/csv-staging/uploads" in compose
    assert "SETUP_CODE: ${SETUP_CODE:-}" in compose
    assert "PRIVACY_POLICY_PATH: ${PRIVACY_POLICY_PATH:-}" in compose
    assert "GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID:-}" in compose
    assert "GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET:-}" in compose
    assert "FIELD_ENCRYPTION_KEY: ${FIELD_ENCRYPTION_KEY:?FIELD_ENCRYPTION_KEY must be set}" in compose
    assert "SIMPLEFIN_SYNC_CRON: ${SIMPLEFIN_SYNC_CRON:-30 6 * * *}" in compose
    assert "simplefin-sync:" in compose
    assert 'entrypoint: ["/app/scripts/run-simplefin-sync.sh"]' in compose
    assert "ai-jobs:" in compose
    assert 'entrypoint: ["/app/scripts/run-ai-jobs.sh"]' in compose
    assert "AI_LOCAL_QUIET_WINDOW: ${AI_LOCAL_QUIET_WINDOW:-22:00-06:00}" in compose
    assert "tmpfs:" in compose
    assert "- /run/csv-staging:size=128m,mode=1777" in compose
    assert "BACKUP_STATUS_PATH: /backups/status" in compose
    assert "OPERATOR_USERNAMES: ${OPERATOR_USERNAMES:-}" in compose
    assert "OFFSITE_RCLONE_REMOTE: ${OFFSITE_RCLONE_REMOTE:-}" in compose
    assert "OFFSITE_AGE_RECIPIENT: ${OFFSITE_AGE_RECIPIENT:-}" in compose
    assert "RCLONE_CONFIG: /config/rclone.conf" in compose


def test_dockerfile_builds_css_with_a_pinned_checksum_and_collectstatic():
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    pins = (Path(__file__).resolve().parent.parent / "scripts" / "css_pins.env").read_text()

    assert "FROM debian:bookworm-slim AS css" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "collectstatic --noinput" in dockerfile
    assert "run-ai-jobs.sh" in dockerfile
    assert "--ignore src" in dockerfile
    assert "--ignore vendor" not in dockerfile
    assert "tailwindcss" in dockerfile
    assert "TAILWIND_VERSION=v4.3.3" in pins
    assert "DAISYUI_VERSION=v5.7.47" in pins
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in (
        Path(__file__).resolve().parent.parent / "financial_planner" / "settings.py"
    ).read_text()



def test_the_simplefin_scheduler_does_not_inherit_the_web_health_check():
    compose = (Path(__file__).resolve().parents[1] / "compose.yml").read_text(encoding="utf-8")
    scheduler = compose.split("  simplefin-sync:", 1)[1].split("\nvolumes:", 1)[0]

    # It runs no web server, so the image's HTTP probe would always fail.
    assert "healthcheck:\n      disable: true" in scheduler


def test_ai_jobs_container_receives_every_ai_job_setting():
    import re

    root = Path(__file__).resolve().parent.parent
    settings_text = (root / "financial_planner" / "settings.py").read_text()
    names = sorted(
        set(re.findall(r'os\.environ\.get\("((?:AI_|AGENT_HARNESS_)[A-Z_]+)"', settings_text))
    )
    assert names
    compose = (root / "compose.yml").read_text()
    start = compose.index("  ai-jobs:")
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", compose[start + 1 :])
    block = compose[start : start + 1 + following.start()] if following else compose[start:]
    missing = [name for name in names if f"{name}:" not in block]
    assert missing == []


def test_background_workers_wait_for_the_migrated_app():
    import re

    compose = (Path(__file__).resolve().parent.parent / "compose.yml").read_text()
    for service in ("simplefin-sync", "ai-jobs"):
        start = compose.index(f"  {service}:\n")
        following = re.search(r"\n  [a-z][a-z0-9-]*:\n", compose[start + 1 :])
        block = compose[start : start + 1 + following.start()] if following else compose[start:]
        assert re.search(r"depends_on:\n(?:.*\n)*?\s+app:\n\s+condition: service_healthy", block), service
