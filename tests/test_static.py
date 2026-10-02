import hashlib
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import Client, RequestFactory
from django.urls import reverse

from finance.middleware import LoginRequiredExceptStaticMiddleware, _static_prefix

from finance.models import Household, Membership, Person


PASSWORD = "Synthetic-passphrase-42!"


def _member():
    user = get_user_model().objects.create_user(username="nav-member", password=PASSWORD)
    person = Person.objects.create(user=user, display_name="Nav Member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user


@pytest.mark.django_db
def test_compiled_css_and_theme_script_are_public(tmp_path, settings):
    static_dir = tmp_path / "static"
    (static_dir / "dist").mkdir(parents=True)
    (static_dir / "js").mkdir(parents=True)
    (static_dir / "dist" / "app.css").write_text("/* synthetic-app-css */")
    (static_dir / "js" / "theme.js").write_text("/* synthetic-theme-js */")
    collected = tmp_path / "staticfiles"
    settings.STATICFILES_DIRS = [static_dir]
    settings.STATIC_ROOT = collected
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
    settings.WHITENOISE_AUTOREFRESH = True
    call_command("collectstatic", "--noinput", verbosity=0)

    client = Client()
    css = client.get("/static/dist/app.css")
    script = client.get("/static/js/theme.js")
    missing = client.get("/static/not-a-real-file.css")
    css_body = b"".join(css.streaming_content)
    script_body = b"".join(script.streaming_content)

    assert css.status_code == 200
    assert "text/css" in css["Content-Type"]
    assert b"synthetic-app-css" in css_body
    assert script.status_code == 200
    assert b"synthetic-theme-js" in script_body
    assert missing.status_code == 404


@pytest.mark.django_db
def test_default_deny_still_protects_non_static_paths():
    _member()
    client = Client()
    unknown = client.get("/not-a-public-page/")
    transactions = client.get(reverse("transaction-list"))
    invite = client.get(reverse("invite"))
    health = client.get(reverse("health"))
    login = client.get(reverse("login"))

    assert unknown.status_code == 404
    assert transactions.status_code == 302
    assert transactions.url.startswith(reverse("login"))
    assert invite.status_code == 302
    assert invite.url.startswith(reverse("login"))
    assert health.status_code == 200
    assert login.status_code == 200


@pytest.mark.django_db
def test_pages_do_not_request_third_party_hosts(client):
    _member()
    login = client.get(reverse("login"))
    content = login.content.decode()

    assert login.status_code == 200
    assert "cdn." not in content.lower()
    assert "googleapis.com" not in content
    assert "fonts.gstatic.com" not in content
    assert "/static/dist/app.css" in content
    assert "/static/js/theme.js" in content


def test_theme_script_notifies_charts_and_charts_stay_self_hosted():
    root = Path(__file__).resolve().parent.parent
    theme = (root / "static" / "js" / "theme.js").read_text(encoding="utf-8")
    charts = (root / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert "financial-planner:themechange" in theme
    assert "financial-planner:themechange" in charts
    assert "cssVarColor" in charts
    assert "cdn." not in charts.lower()
    assert "https://" not in charts


@pytest.mark.django_db
def test_signed_in_pages_use_shared_nav_and_signed_out_pages_use_a_card():
    user = _member()
    client = Client()
    client.force_login(user)
    home = client.get(reverse("home")).content.decode()
    login = Client().get(reverse("login")).content.decode()

    for label in (
        "Cash flow",
        "Net worth",
        "Spending",
        "Transactions",
        "Transfers",
        "Recurring",
        "Categories",
        "Accounts",
        "Import",
        "Invite",
        "Sign out",
    ):
        assert label in home
    assert 'aria-current="page"' in home
    assert 'id="theme-toggle"' in home
    assert "/static/vendor/chart.umd.min.js" in home
    assert "/static/js/charts.js" in home
    assert "cdn." not in home.lower()
    assert "drawer" in home
    assert "card-body" in login
    assert "drawer" not in login
    assert "<p><a href=" not in home


def test_static_url_prefix_is_normalized(settings):
    settings.STATIC_URL = "static"
    assert _static_prefix() == "/static/"
    settings.STATIC_URL = "/static"
    assert _static_prefix() == "/static/"
    settings.STATIC_URL = None
    assert _static_prefix() == "/static/"


def test_login_required_middleware_skips_static_paths(settings):
    settings.STATIC_URL = "/static/"
    middleware = LoginRequiredExceptStaticMiddleware(lambda request: None)
    request = RequestFactory().get("/static/dist/app.css")
    request.user = AnonymousUser()
    assert middleware.process_view(request, lambda: None, (), {}) is None


def test_vendored_chartjs_matches_recorded_checksum():
    vendor = Path(__file__).resolve().parent.parent / "static" / "vendor"
    recorded = {}
    for line in (vendor / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, name = line.split()
        recorded[name] = digest
    blob = (vendor / "chart.umd.min.js").read_bytes()

    assert recorded["chart.umd.min.js"] == hashlib.sha256(blob).hexdigest()
    assert b"Chart.js v4.5.1" in blob
    assert b"window.Chart" in blob
    assert b"sourceMappingURL" not in blob
    assert (vendor / "LICENSE.md").read_text(encoding="utf-8").startswith("The MIT License")


@pytest.mark.django_db
def test_collectstatic_accepts_vendored_chartjs_without_a_source_map(tmp_path, settings):
    root = Path(__file__).resolve().parent.parent
    static_dir = tmp_path / "static"
    vendor = static_dir / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "chart.umd.min.js").write_bytes((root / "static" / "vendor" / "chart.umd.min.js").read_bytes())
    collected = tmp_path / "staticfiles"
    settings.STATICFILES_DIRS = [static_dir]
    settings.STATIC_ROOT = collected
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
    call_command("collectstatic", "--noinput", verbosity=0)

    hashed = list(collected.rglob("chart.umd.min.js*"))
    assert hashed
    assert not any(path.name.endswith(".map") for path in collected.rglob("*"))


