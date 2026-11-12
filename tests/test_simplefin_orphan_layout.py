"""Unlinking a SimpleFIN link the latest read no longer reports, in Chromium, desktop and phone (#736).

fetch_accounts is a synthetic feed, so nothing leaves the test.
"""

from datetime import date

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from finance.models import Account, AccountLink, Person, Transaction
from finance.simplefin_services import listed_accounts, save_account_links, store_read, sync_connection
from tests.a11y import assert_accessible
from tests.browser_support import CSS, browser, page_session  # noqa: F401
from tests.test_simplefin import account_payload, connect_owner, posted_txn

pytestmark = pytest.mark.django_db(transaction=True)

VIEWPORTS = {"desktop": (1440, 900), "phone": (375, 667)}
HEADING = "No longer reported by SimpleFIN"


def _orphan_for(person, monkeypatch):
    account = Account.objects.create(name="Synthetic Dropped Checking", account_type=Account.Type.CHECKING, owner=person)
    payload = account_payload(transactions=[posted_txn(txn_id="sf-ui-1", day=10, amount="-5.00")])
    connection = connect_owner(person, monkeypatch, payload)
    save_account_links(person, connection.pk, [{
        "simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": account.pk,
        "cutover_date": date(2026, 3, 1),
    }])
    sync_connection(person, connection.pk, ignore_rate_limit=True)
    later = account_payload(account_id="sf-still-reported")
    later["accounts"][0]["name"] = "Synthetic Still Reported"
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *args, **kwargs: later)
    store_read(connection.pk, later, listed_accounts(later), timezone.now())
    return account, connection


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_an_orphaned_link_can_be_unlinked(page_session, monkeypatch, viewport):  # noqa: F811 - pytest fixture
    person = Person.objects.get(user__username="synthetic_alex")
    account, connection = _orphan_for(person, monkeypatch)
    context, page = page_session(VIEWPORTS[viewport], "light", "/settings/connections/")
    try:
        section = page.get_by_role("region", name=HEADING)
        expect(section).to_be_visible()
        expect(section).to_contain_text("Synthetic Dropped Checking")
        expect(section).to_contain_text("2026-03-10")
        expect(section).to_contain_text("2026-03-01")
        choice = section.get_by_label("Keep or unlink Synthetic Dropped Checking")
        expect(choice).to_have_value("keep")
        if CSS.exists():  # Width needs the stylesheet; CI always builds it before these tests.
            assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        assert_accessible(page.content())

        choice.select_option("unlink")
        section.get_by_role("button", name="Save").click()
        page.wait_for_load_state("networkidle")

        expect(page.get_by_text("Unlinked 1 account", exact=False)).to_be_visible()
        expect(page.get_by_role("region", name=HEADING)).to_have_count(0)
        page.reload()
        expect(page.get_by_role("region", name=HEADING)).to_have_count(0)
        assert not AccountLink.objects.filter(connection=connection).exists()
        assert Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE).count() == 1
    finally:
        context.close()
