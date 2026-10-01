import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

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


@pytest.mark.django_db
def test_signed_in_pages_use_shared_nav_and_signed_out_pages_use_a_card():
    user = _member()
    client = Client()
    client.force_login(user)
    home = client.get(reverse("home")).content.decode()
    login = Client().get(reverse("login")).content.decode()

    for label in (
        "Cash flow",
        "Spending",
        "Transactions",
        "Transfers",
        "Recurring",
        "Categories",
        "Import",
        "Invite",
        "Sign out",
    ):
        assert label in home
    assert 'aria-current="page"' in home
    assert 'id="theme-toggle"' in home
    assert "drawer" in home
    assert "card-body" in login
    assert "drawer" not in login
    assert "<p><a href=" not in home

