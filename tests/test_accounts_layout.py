"""Accounts page at desktop widths in a real browser (issue #332, Desktop 6), and the account
sort control on Accounts and Net worth at phone and desktop widths (#655).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise. The list, add
form and header link are checked with the Goals and Planned items pages in
test_goals_import_alerts_layout.py.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from finance.models import Account, BalanceSnapshot
from tests.mobile_seed import seed_phone_data

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as error:  # noqa: BLE001 - browser binaries are optional locally
            pytest.skip(f"Chromium is not installed: {error}")
        yield instance
        instance.close()


@pytest.fixture
def accounts_page(live_server, client, browser):
    person = seed_phone_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def open_page(width, scheme="light"):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + "/accounts/")
        page.wait_for_load_state("networkidle")
        return page

    yield open_page
    for context in contexts:
        context.close()


def test_row_menu_reaches_rename_and_archive(accounts_page):
    page = accounts_page(1440)
    page.get_by_label("More actions for Synthetic Checking").click()
    menu = page.locator("details[data-row-menu][open]")
    menu.locator("summary", has_text="Rename").click()
    expect(page.get_by_label("New name for Synthetic Checking")).to_be_visible()

    menu.get_by_role("button", name="Archive").click()
    expect(page.get_by_role("dialog")).to_contain_text("Archive Synthetic Checking?")


PAGES = {"accounts": "/accounts/", "net_worth": "/net-worth/"}
VIEWPORTS = {"phone": (390, 844), "desktop": (1440, 900)}

CONTROL_JS = """(page) => {
  const form = document.querySelector(`form[data-account-sort="${page}"]`);
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.width, r.height, r.right]; };
  const select = form.querySelector('select');
  const style = getComputedStyle(select);
  return {
    select: box(select),
    button: box(form.querySelector('button[type=submit]')),
    label: form.querySelector('label').textContent.trim(),
    color: style.color,
    background: style.backgroundColor,
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    width: document.documentElement.clientWidth,
  };
}"""


def _seed():
    """The phone seed plus balances on two assets and a debt, so both lists have rows to sort."""
    person = seed_phone_data()
    today = timezone.localdate()
    card = Account.objects.create(
        name="Synthetic Card", account_type=Account.Type.CREDIT_CARD, owner=person, scope=Account.Scope.PRIVATE
    )
    for name, amount in (("Synthetic Checking", 120_000), ("Synthetic Investment", 900_000)):
        account = Account.objects.get(name=name)
        BalanceSnapshot.objects.create(
            account=account, snapshot_date=today, amount_minor=amount, currency="USD", source="manual"
        )
    BalanceSnapshot.objects.create(
        account=card, snapshot_date=today, amount_minor=45_000, currency="USD", source="manual"
    )
    return person


@pytest.fixture
def open_page(live_server, client, browser):
    person = _seed()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def opener(size, scheme, path, javascript=True):
        width, height = VIEWPORTS[size]
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=scheme,
            java_script_enabled=javascript,
            has_touch=size == "phone",
        )
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        return page

    yield opener
    for context in contexts:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", VIEWPORTS)
@pytest.mark.parametrize("page_name", PAGES)
def test_sort_control_fits_and_has_44px_targets(open_page, page_name, size, scheme):
    page = open_page(size, scheme, PAGES[page_name])
    layout = page.evaluate(CONTROL_JS, page_name)
    assert not layout["scroll"], layout
    for target in ("select", "button"):
        width, height, right = layout[target]
        assert width >= 44 and height >= 44, (target, layout)
        assert right <= layout["width"], (target, layout)
    assert layout["color"] != layout["background"], layout
    options = page.locator(f'form[data-account-sort="{page_name}"] select option').all_inner_texts()
    assert [text.strip() for text in options] == [
        "Default order", "Name A–Z", "Name Z–A", "Balance high→low", "Balance low→high",
    ]


def _panel_names(page):
    return [
        text.strip()
        for text in page.locator("section[aria-labelledby=net-worth-accounts] ul").first.locator(
            "li > span:first-child > span:first-child"
        ).all_inner_texts()
    ]


@pytest.mark.parametrize("size", VIEWPORTS)
def test_sort_works_without_javascript(open_page, size):
    page = open_page(size, "light", "/net-worth/", javascript=False)
    form = page.locator('form[data-account-sort="net_worth"]')
    form.locator("select").select_option("balance_asc")
    with page.expect_navigation():
        form.get_by_role("button", name="Sort").click()
    assert page.url.endswith("/net-worth/#net-worth-accounts")
    names = _panel_names(page)
    assert names[0].startswith("Synthetic Card") and names[-1].startswith("Synthetic Investment"), names
    assert form.locator("select").input_value() == "balance_asc"

    page.goto(page.url.split("/net-worth/")[0] + "/accounts/")
    accounts = page.locator('form[data-account-sort="accounts"]')
    assert accounts.locator("select").input_value() == ""
    accounts.locator("select").select_option("name_desc")
    with page.expect_navigation():
        accounts.get_by_role("button", name="Sort").click()
    assert page.locator('form[data-account-sort="accounts"] select').input_value() == "name_desc"
