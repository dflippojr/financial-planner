import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import DatabaseError, connections
from django.test import override_settings
from django.urls import reverse

from financial_planner.healthcheck import probe_host
from financial_planner.settings import allowed_hosts_from_env
from tests.helpers import compose_service


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


def test_gunicorn_timeout_outlasts_the_agent_harness_chat_wait():
    # Chat waits for the harness session inside the request. With gunicorn's
    # default 30 s timeout the worker was killed mid-answer and chat returned 500.
    script = (Path(__file__).resolve().parent.parent / "scripts/start-production.sh").read_text()
    timeout_line = next(line for line in script.splitlines() if "--timeout" in line)
    default = int(timeout_line.split(":-", 1)[1].split("}", 1)[0])

    assert default > settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS


def test_wsgi_preload_opens_no_database_connection_before_fork():
    import subprocess
    import sys

    # Run in a fresh interpreter: this pytest process has already set up Django.
    result = subprocess.run(
        [sys.executable, "-c", """
from unittest.mock import patch
from django.db.backends.base.base import BaseDatabaseWrapper
with patch.object(BaseDatabaseWrapper, 'ensure_connection', side_effect=AssertionError('DB opened before fork')):
    import financial_planner.wsgi
from django.db import connections
assert all(conn.connection is None for conn in connections.all())
"""],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_compose_stages_csv_uploads_on_a_memory_backed_mount():
    # An abandoned upload of a real bank export must never sit on disk, so the
    # staging directory has to live on a tmpfs and the app has to be pointed at it.
    app = compose_service("app")

    assert "CSV_IMPORT_STAGING_DIR: /run/csv-staging/uploads" in app
    assert "FIELD_ENCRYPTION_KEY: ${FIELD_ENCRYPTION_KEY:?FIELD_ENCRYPTION_KEY must be set}" in app
    assert "tmpfs:" in app
    assert "- /run/csv-staging:size=128m,mode=1777" in app


@pytest.mark.parametrize(
    ("services", "sources", "variable_pattern"),
    [
        # The Django settings and app code read these; the job tuning ones go to the background worker only.
        (("app", "background"), ("finance/**/*.py", "financial_planner/**/*.py"), r"""(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Z][A-Z0-9_]+)["']"""),
        (("backup",), ("ops/backup/*.sh",), r"\$\{?([A-Z][A-Z0-9_]*)"),
    ],
    ids=["app code", "backup scripts"],
)
def test_documented_settings_reach_the_container_that_reads_them(services, sources, variable_pattern):
    root = Path(__file__).resolve().parent.parent
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (root / ".env.example").read_text(encoding="utf-8"), re.M))
    read = {
        name
        for pattern in sources
        for path in root.glob(pattern)
        for name in re.findall(variable_pattern, path.read_text(encoding="utf-8"))
    }
    passed = {
        name for service in services for name in re.findall(r"^      ([A-Z][A-Z0-9_]*):", compose_service(service), re.M)
    }

    assert read & documented
    assert sorted((read & documented) - passed) == []


def test_backup_container_mounts_only_the_rclone_config_file():
    root = Path(__file__).resolve().parent.parent
    compose = (root / "compose.yml").read_text()
    env_example = (root / ".env.example").read_text()
    placeholder = root / "ops" / "backup" / "rclone.conf.example"

    assert "CONFIG_DIR" not in compose
    assert "D:/financial-planner-config" not in compose
    assert "CONFIG_DIR" not in env_example
    assert "OFFSITE_RCLONE_CONFIG:-./ops/backup/rclone.conf.example" in compose
    assert "target: /config/rclone.conf" in compose
    assert "OFFSITE_RCLONE_CONFIG=" in env_example
    assert placeholder.is_file()
    assert placeholder.read_text(encoding="utf-8").lstrip().startswith("#")


def test_app_and_simplefin_mount_backup_health_not_the_dump_directory():
    app = compose_service("app")
    scheduler = compose_service("background")
    backup = compose_service("backup")

    for block in (app, scheduler):
        assert "BACKUP_STATUS_PATH: /backup-health/status" in block
        assert "source: ${BACKUP_DIR:?BACKUP_DIR must be set}/health" in block
        assert "target: /backup-health" in block
        assert "create_host_path: true" in block.split("target: /backup-health", 1)[1].split("entrypoint", 1)[0]
        assert "source: ${BACKUP_DIR:?BACKUP_DIR must be set}\n" not in block
    assert "source: ${BACKUP_DIR:?BACKUP_DIR must be set}\n" in backup
    assert "/health" not in backup.split("volumes:", 1)[1].split("OFFSITE_RCLONE_CONFIG", 1)[0]


def test_dockerfile_builds_css_with_a_pinned_checksum_and_collectstatic():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text()
    pins = (root / "scripts" / "css_pins.env").read_text()

    assert "FROM debian:bookworm-slim AS css" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "collectstatic --noinput" in dockerfile
    assert "run-background.sh" in dockerfile
    assert "--ignore src" in dockerfile
    assert "--ignore vendor" not in dockerfile
    assert "tailwindcss" in dockerfile
    # Every pin the build uses is defined in the pins file, whichever version it holds.
    used = set(re.findall(r"\$\{?((?:TAILWIND|DAISYUI)_[A-Z0-9_]+)", dockerfile))
    defined = set(re.findall(r"^((?:TAILWIND|DAISYUI)_[A-Z0-9_]+)=\S+$", pins, re.M))
    assert "TAILWIND_VERSION" in used
    assert used <= defined
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE


def test_the_simplefin_scheduler_does_not_inherit_the_web_health_check():
    scheduler = compose_service("background")

    # It runs no web server, so the image's HTTP probe would always fail.
    assert "healthcheck:\n      disable: true" in scheduler


def test_ai_jobs_container_receives_every_ai_job_setting():
    root = Path(__file__).resolve().parent.parent
    settings_text = (root / "financial_planner" / "settings.py").read_text()
    names = sorted(
        set(re.findall(r'os\.environ\.get\("((?:AI_|AGENT_HARNESS_)[A-Z_]+)"', settings_text))
    )
    assert names
    block = compose_service("background")
    missing = [name for name in names if f"{name}:" not in block]
    assert missing == []


def test_background_workers_wait_for_the_migrated_app():
    block = compose_service("background")
    assert re.search(r"depends_on:\n(?:.*\n)*?\s+app:\n\s+condition: service_healthy", block)


def test_database_backup_and_test_images_share_one_postgres_major():
    root = Path(__file__).resolve().parent.parent
    compose = (root / "compose.yml").read_text()
    db = compose.split("  db:\n", 1)[1].split("\n  app:", 1)[0]
    majors = {
        "compose db": re.findall(r"image: postgres:(\d+)-alpine", db),
        "backup image": re.findall(r"^FROM postgres:(\d+)-alpine", (root / "ops/backup/Dockerfile").read_text(), re.M),
        "test_postgres.sh": re.findall(r"postgres:(\d+)-alpine", (root / "scripts/test_postgres.sh").read_text()),
    }
    assert all(len(found) == 1 for found in majors.values()), majors
    assert len({found[0] for found in majors.values()}) == 1, majors
    # 18+ images keep data in a versioned subdirectory and refuse a mount at .../data.
    if int(majors["compose db"][0]) >= 18:
        assert "- postgres_data:/var/lib/postgresql\n" in db


@pytest.mark.parametrize("script", ["row-counts.sh", "verify-restore.sh"])
def test_backup_image_ships_the_script_the_deployment_runbook_uses(script):
    root = Path(__file__).resolve().parent.parent
    copy_line = next(line for line in (root / "ops/backup/Dockerfile").read_text().splitlines() if line.startswith("COPY"))
    assert script in copy_line
    assert f"/opt/financial-planner/{script}" in (root / "docs/deployment.md").read_text(encoding="utf-8")


def test_backup_schedule_defaults_agree_and_avoid_daylight_saving_hours():
    # BusyBox crond skips 02:00-02:59 on spring-forward Sunday and repeats
    # 01:00-01:59 on fall-back Sunday (#512).
    root = Path(__file__).resolve().parent.parent
    defaults = {
        re.search(r"BACKUP_CRON:-([^}]+)\}", (root / "compose.yml").read_text()).group(1),
        re.search(r"BACKUP_CRON:-([^}]+)\}", (root / "ops/backup/run-backups.sh").read_text()).group(1),
        re.search(r"^BACKUP_CRON=(.+)$", (root / ".env.example").read_text(), re.MULTILINE).group(1).strip(),
    }

    assert defaults == {"30 3 * * *"}
