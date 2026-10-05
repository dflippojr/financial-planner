from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.urls import reverse

from finance.ai_services import connect_harness
from finance.models import Account
from tests.test_bulk_edit import make_account, make_household, make_person, make_transaction
from tests.test_chat import TOKEN, harness, make_member  # noqa: F401 - harness is a fixture


TEMPLATES_DIR = Path(django_settings.BASE_DIR) / "templates"


class InlineCodeFinder(HTMLParser):
    """Collect inline scripts, inline <style> blocks, and on*= handler attributes."""

    def __init__(self):
        super().__init__()
        self.problems = []

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        if tag == "script" and not names.get("src") and names.get("type") != "application/json":
            self.problems.append(f"inline <script> at line {self.getpos()[0]}")
        if tag == "style":
            self.problems.append(f"inline <style> at line {self.getpos()[0]}")
        for name, _value in attrs:
            if name.startswith("on"):
                self.problems.append(f"{name}= on <{tag}> at line {self.getpos()[0]}")


def inline_code(html):
    finder = InlineCodeFinder()
    finder.feed(html)
    finder.close()
    return finder.problems


def assert_page_is_csp_clean(response):
    assert response.status_code == 200, response
    assert inline_code(response.content.decode()) == []
    assert response["Content-Security-Policy"] == django_settings.CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == django_settings.PERMISSIONS_POLICY


def test_no_template_has_inline_scripts_or_handlers():
    found = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        problems = inline_code(path.read_text(encoding="utf-8"))
        if problems:
            found[str(path.relative_to(TEMPLATES_DIR))] = problems

    assert found == {}


def test_finder_flags_the_patterns_the_policy_blocks():
    html = (
        '<script>alert(1)</script><script src="/static/js/a.js"></script>'
        '<script type="application/json">{}</script><style>p{}</style>'
        '<form onsubmit="return confirm(1)"><button onclick="go()">x</button></form>'
    )

    problems = inline_code(html)

    assert len(problems) == 4
    assert any("onsubmit" in problem for problem in problems)
    assert any("onclick" in problem for problem in problems)


@pytest.mark.django_db
def test_main_member_pages_render_without_inline_code():
    owner = make_person("csp-owner")
    household = make_household(owner)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    transaction = make_transaction(owner, shared)
    client = Client()
    client.force_login(owner.user)
    pages = [
        reverse("home"),
        reverse("spending-by-category"),
        reverse("net-worth"),
        reverse("debt-payoff"),
        reverse("account-list"),
        reverse("account-balances", args=(shared.pk,)),
        reverse("transaction-list"),
        reverse("transaction-edit", args=(transaction.pk,)),
        reverse("csv-import"),
        reverse("budgets"),
        reverse("savings-goals"),
        reverse("bills-calendar"),
        reverse("recurring-review"),
        reverse("planned-items"),
        reverse("monthly-review"),
        reverse("sheet-comparison"),
        reverse("year-end"),
        reverse("transfer-review"),
        reverse("alert-list"),
        reverse("chat"),
        reverse("account-settings"),
        reverse("simplefin-connections"),
        reverse("invite"),
        reverse("category-list"),
        reverse("category-rule-list"),
        reverse("tag-list"),
        reverse("csv-mapping-list"),
        reverse("settings-alerts"),
        reverse("settings-data"),
        reverse("settings-ai"),
    ]

    for url in pages:
        assert_page_is_csp_clean(client.get(url))


@pytest.mark.django_db
def test_page_actions_replace_inline_handlers():
    owner = make_person("csp-actions")
    household = make_household(owner)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    lent = make_account(owner, name="Synthetic Lent", scope=Account.Scope.HOUSEHOLD, household=household)
    Account.objects.filter(pk=lent.pk).update(share_mode=Account.ShareMode.LENT)
    transaction = make_transaction(owner, shared)
    client = Client()
    client.force_login(owner.user)

    accounts = client.get(reverse("account-list")).content.decode()
    imports = client.get(reverse("csv-import")).content.decode()
    # The delete form only renders once sheet totals exist; check the template itself.
    comparison = (TEMPLATES_DIR / "finance" / "sheet_comparison.html").read_text(encoding="utf-8")
    transactions = client.get(reverse("transaction-list")).content.decode()
    edit = client.get(reverse("transaction-edit", args=(transaction.pk,))).content.decode()

    assert "js/page-actions.js" in accounts
    assert f'data-open-dialog="co-own-{lent.pk}"' in accounts
    assert f'id="co-own-{lent.pk}"' in accounts
    assert f'data-open-dialog="archive-{shared.pk}"' in accounts
    assert f'id="archive-{shared.pk}"' in accounts
    assert 'data-confirm="Undoing this import deletes receipts on the rows it removes."' in imports
    assert 'data-confirm="Delete all stored sheet totals and the remembered mapping?"' in comparison
    assert "js/bulk-select.js" in transactions
    assert 'id="select-page"' in transactions
    assert "js/transaction-split.js" in edit
    assert 'id="split-form"' in edit


@pytest.mark.django_db
def test_chat_page_keeps_confirm_text_without_inline_code(harness):  # noqa: F811
    _state, url = harness
    _user, person, _household = make_member("csp-chat")
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(person.user)

    response = client.get(reverse("chat"))

    assert_page_is_csp_clean(response)
    assert 'data-confirm="Delete every conversation?"' in response.content.decode()


@pytest.mark.django_db
def test_sign_in_setup_join_and_recovery_pages_carry_the_policy():
    client = Client()

    assert_page_is_csp_clean(client.get(reverse("setup")))
    make_person("csp-first")
    for name in ("login", "join", "recover", "privacy-policy"):
        assert_page_is_csp_clean(client.get(reverse(name)))


@pytest.mark.django_db
def test_headers_cover_redirects_errors_and_static_responses():
    client = Client()

    redirect = client.get(reverse("transaction-list"))
    missing = client.get("/no-such-page/")
    health = client.get(reverse("health"))

    for response in (redirect, missing, health):
        assert response["Content-Security-Policy"] == django_settings.CONTENT_SECURITY_POLICY
        assert response["Permissions-Policy"] == django_settings.PERMISSIONS_POLICY


def test_default_policies_match_the_agreed_strings():
    policy = dict(
        part.strip().split(" ", 1) for part in django_settings.CONTENT_SECURITY_POLICY.split(";")
    )

    assert policy == {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src-elem": "'self'",
        "style-src-attr": "'unsafe-inline'",
        "img-src": "'self' data:",
        "font-src": "'self'",
        "connect-src": "'self'",
        "object-src": "'none'",
        "base-uri": "'self'",
        "form-action": "'self' https://accounts.google.com",
        "frame-ancestors": "'none'",
    }
    assert django_settings.PERMISSIONS_POLICY == (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )


@pytest.mark.django_db
def test_empty_setting_sends_no_header(settings):
    settings.CONTENT_SECURITY_POLICY = ""
    settings.PERMISSIONS_POLICY = ""

    response = Client().get(reverse("health"))

    assert response.status_code == 200
    assert "Content-Security-Policy" not in response
    assert "Permissions-Policy" not in response


@pytest.mark.django_db
def test_operator_policy_replaces_default(settings):
    settings.CONTENT_SECURITY_POLICY = "default-src 'self'; img-src 'self' https://example.test"
    settings.PERMISSIONS_POLICY = "camera=()"

    response = Client().get(reverse("health"))

    assert response["Content-Security-Policy"] == settings.CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == "camera=()"


def test_middleware_keeps_a_header_the_view_already_set(rf):
    from django.http import HttpResponse

    from finance.middleware import SecurityPolicyHeadersMiddleware

    def view(_request):
        response = HttpResponse("ok")
        response["Content-Security-Policy"] = "default-src 'none'"
        return response

    response = SecurityPolicyHeadersMiddleware(view)(rf.get("/"))

    assert response["Content-Security-Policy"] == "default-src 'none'"
    assert response["Permissions-Policy"] == django_settings.PERMISSIONS_POLICY
