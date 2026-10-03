import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.models import Household, Membership, Person


PASSWORD = "Synthetic-passphrase-42!"


def _member():
    user = get_user_model().objects.create_user(username="settings-member", password=PASSWORD)
    person = Person.objects.create(user=user, display_name="Settings Member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user


@pytest.mark.django_db
def test_old_settings_urls_permanently_redirect():
    user = _member()
    client = Client()
    client.force_login(user)
    cases = [
        ("/account/", reverse("account-settings")),
        ("/account/export/", reverse("account-export")),
        ("/account/ai/connect/", reverse("ai-connect")),
        ("/account/ai/disconnect/", reverse("ai-disconnect")),
        ("/account/ai/defaults/", reverse("ai-defaults")),
        ("/invite/", reverse("invite")),
        ("/household/leave/", reverse("leave-household")),
        ("/accounts/connections/", reverse("simplefin-connections")),
        ("/accounts/connections/sync/", reverse("simplefin-sync")),
        ("/accounts/connections/disconnect/", reverse("simplefin-disconnect")),
        ("/categories/", reverse("category-list")),
        ("/categories/rules/", reverse("category-rule-list")),
        ("/categories/rules/7/", reverse("category-rule-detail", args=(7,))),
        (
            "/categories/rules/applications/9/reverse/",
            reverse("category-rule-application-reverse", args=(9,)),
        ),
    ]
    for old_path, new_path in cases:
        response = client.get(old_path)
        assert response.status_code == 301, old_path
        assert response.url == new_path


@pytest.mark.django_db
def test_settings_tabs_and_footer_are_active_on_subpages():
    user = _member()
    client = Client()
    client.force_login(user)
    pages = [
        (reverse("account-settings"), "Sign-in &amp; security"),
        (reverse("simplefin-connections"), "Connections"),
        (reverse("invite"), "Household"),
        (reverse("category-list"), "Categories"),
        (reverse("category-rule-list"), "Categories"),
        (reverse("tag-list"), "Tags"),
        (reverse("settings-data"), "Data"),
        (reverse("settings-ai"), "AI"),
    ]
    for url, label in pages:
        page = client.get(url)
        html = page.content.decode()
        assert page.status_code == 200, url
        assert 'aria-label="Settings"' in html
        assert 'aria-current="page"' in html
        assert f">{label}<" in html
        assert 'role="tablist"' in html
        assert "menu-active" not in html
        for other in ("Cash flow", "Accounts", "Import"):
            assert other in html


@pytest.mark.django_db
def test_household_tab_lists_members():
    user = _member()
    client = Client()
    client.force_login(user)
    page = client.get(reverse("invite"))
    assert page.status_code == 200
    assert b"Settings Member" in page.content
    assert b"Create invitation code" in page.content
    assert b"Leave household" in page.content
    assert reverse("invite") == "/settings/household/"
    assert reverse("category-list") == "/settings/categories/"
    assert reverse("simplefin-connections") == "/settings/connections/"


@pytest.mark.django_db
def test_settings_data_tab_is_read_only():
    user = _member()
    client = Client()
    client.force_login(user)

    assert client.post(reverse("settings-data")).status_code == 405


@pytest.mark.django_db
def test_old_tags_url_redirects_to_the_settings_tab():
    user = _member()
    client = Client()
    client.force_login(user)

    response = client.get("/tags/")

    assert response.status_code == 301
    assert response["Location"] == reverse("tag-list")


@pytest.mark.django_db
def test_csv_mappings_live_on_a_settings_tab_and_old_url_redirects():
    user = _member()
    client = Client()
    client.force_login(user)

    page = client.get(reverse("csv-mapping-list"))
    assert page.status_code == 200
    assert ">CSV mappings<" in page.content.decode()
    assert 'aria-current="page"' in page.content.decode()
    old = client.get("/csv-mappings/")
    assert old.status_code == 301
    assert old["Location"] == reverse("csv-mapping-list")
