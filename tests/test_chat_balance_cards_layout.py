"""Balance cards (#660) in Chromium, at phone width in light and desktop width in dark.

A card with a long account name, a credit card row with its sign explained and a refused future-dated
row stays inside the reply without sideways scrolling; tickable rows are labels at least 44 px tall,
the refused row shows its reason with no checkbox, and applying records only the ticked balance.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from finance.chat_balance_cards import LIABILITY_SIGN
from finance.chat_proposals import proposal_tools
from finance.chat_services import send_message
from finance.models import Account, AiConversationMessage, BalanceSnapshot
from tests.browser_support import browser, page_session  # noqa: F401
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_cleanup_cards_layout import LAYOUT_JS
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

LONG_NAME = "SYN-" + "Brokerage" * 10


def _accounts(person):
    def make(name, account_type):
        return Account.objects.create(name=name, account_type=account_type, owner=person, scope=Account.Scope.PRIVATE)

    return make(LONG_NAME, Account.Type.INVESTMENT), make("SYN Rewards Card", Account.Type.CREDIT_CARD)


def _propose_and_answer(person, conversation, brokerage, card):
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    tools = {spec.name: spec for spec in proposal_tools(conversation, turn)}
    tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
    assert tools["propose_balances"].handler(person, {"items": [
        {"account_id": brokerage.pk, "date": "2026-03-31", "amount_minor": 1050000, "net_contribution_minor": 50000,
         "note": "statement.pdf page 2"},
        {"account_id": card.pk, "date": "2026-03-31", "amount_minor": 81240},
        {"account_id": card.pk, "date": tomorrow, "amount_minor": 1},
    ]}).ok
    turn.status = AiConversationMessage.Status.DONE
    turn.content = "Here are the balances from your statement. Nothing changes until you apply."
    turn.save(update_fields=("status", "content"))


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_balance_card_layout_and_partial_apply(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        brokerage, card = _accounts(person)
        conversation = send_message(person, "Record the balances from my statement.")
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        _propose_and_answer(person, conversation, brokerage, card)
        balances = page.locator('main [data-chat-proposal="balances"]')
        expect(balances).to_be_visible(timeout=15000)
        page.wait_for_load_state("networkidle")

        layout = {found["kind"]: found for found in page.evaluate(LAYOUT_JS)}
        assert set(layout) == {"balances"}
        found = layout["balances"]
        assert found["inside"] and not found["pageScroll"] and all(row["inside"] for row in found["rows"])
        first, second, refused = found["rows"]
        assert first["label"] >= 44 and second["label"] >= 44 and not first["problem"] and not second["problem"]
        assert refused == {"label": 0, "problem": True, "inside": True}
        expect(balances).to_contain_text("Amount owed 812.40 USD")
        expect(balances).to_contain_text(LIABILITY_SIGN)
        expect(balances).to_contain_text("Balance date cannot be in the future.")

        balances.locator('[data-proposal-item="1"] label').click()
        expect(balances.locator('[data-proposal-item="1"] input')).not_to_be_checked()
        balances.get_by_role("button", name="Apply ticked items").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator('main [data-chat-proposal="balances"] [role="status"]')).to_have_text(
            "Applied 1 of 3 items.")
        recorded = BalanceSnapshot.objects.get()
        assert (recorded.account_id, recorded.amount_minor, recorded.net_contribution_minor) == (
            brokerage.pk, 1050000, 50000)
        assert not page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
    finally:
        context.close()
