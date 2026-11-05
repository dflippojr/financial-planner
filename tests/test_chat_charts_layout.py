"""Chat charts in Chromium (#646): all four kinds draw on live and saved turns, in the chat page and the popover.

The charts are made by make_chart from the seeded member's data, then stored on the turn the way the chat
runner stores them. No chat lane runs beside the live server, so each reply stays pending until the test
answers it; the page then swaps the reply in live, and a reload shows the saved turn.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from finance.chat_charts import make_chart
from finance.chat_services import send_message
from finance.models import Account, AiConversation, AiConversationMessage, Category, ImportBatch, Transaction
from tests.browser_support import SCREEN_DIR, browser, page_session  # noqa: F401
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

# Collects CSP violations before any page script runs.
CSP_WATCH = "window.__cspViolations = []; document.addEventListener('securitypolicyviolation', (e) => " \
            "window.__cspViolations.push(e.violatedDirective + ' ' + e.blockedURI));"

LAYOUT_JS = """(scope) => {
  const root = document.documentElement;
  return Array.from(document.querySelectorAll(scope + ' [data-chat-chart]'), (figure) => {
    const article = figure.closest('article').getBoundingClientRect();
    const canvas = figure.querySelector('canvas');
    const box = canvas.getBoundingClientRect();
    const chart = window.Chart && window.Chart.getChart(canvas);
    const legend = chart && chart.options.plugins.legend.labels.color;
    return {
      drawn: !!chart,
      kind: JSON.parse(figure.querySelector('script[type="application/json"]').textContent).kind,
      sized: box.width > 100 && box.height > 100,
      inside: box.left >= article.left - 0.5 && box.right <= article.right + 0.5,
      themed: legend === getComputedStyle(document.body).color,
      tableRows: figure.querySelectorAll('.sr-only table tbody tr').length,
      labels: chart ? chart.data.labels.length : -1,
      link: figure.querySelector('a').getAttribute('href'),
      pageScroll: root.scrollWidth > root.clientWidth,
    };
  });
}"""
# The server-rendered parts of each figure; Chart.js only adds size attributes to the canvas.
MARKUP_JS = """(scope) => Array.from(document.querySelectorAll(scope + ' [data-chat-chart]'), (figure) =>
  [figure.querySelector('figcaption').outerHTML, figure.querySelector('script').textContent,
   figure.querySelector('table').outerHTML, figure.querySelector('canvas').getAttribute('aria-label'),
   figure.querySelector('a').outerHTML].join('\\n'))"""


def _history(person):
    """Six months of categorised spending on the member's own account, so every chart kind has data."""
    account = Account.objects.get(name="Synthetic Checking")
    batch = ImportBatch.objects.filter(account=account).first()
    categories = list(Category.objects.filter(name__in=("Groceries", "Dining", "Transportation")).order_by("name"))
    today = timezone.localdate()
    for month_back in range(6):
        day = (today.replace(day=1) - timedelta(days=31 * month_back)).replace(day=2)
        for index, category in enumerate(categories):
            Transaction.objects.create(
                account=account, import_batch=batch, transaction_date=day,
                amount_minor=-(4000 + 777 * index + 191 * month_back), description=f"SYN chart row {index}",
                kind=Transaction.Kind.CASH_FLOW, category=category, source_row_number=500 + month_back * 10 + index,
                fingerprint=f"chart{month_back}{index}".encode().hex().ljust(64, "d"),
                original_fields={"synthetic": "chart"},
            )


def _charts(person):
    _history(person)
    today = timezone.localdate()
    month = today.replace(day=1)
    last_month = (month - timedelta(days=1)).replace(day=1)
    start = (month - timedelta(days=31 * 5)).replace(day=1)
    requests = (
        {"kind": "over_time", "report": "spending", "date_from": start.isoformat(), "date_to": today.isoformat()},
        {"kind": "category_breakdown", "report": "spending", "date_from": start.isoformat(), "date_to": today.isoformat()},
        {"kind": "budget_vs_actual", "report": "budgets", "date_from": month.isoformat()},
        {"kind": "compare_periods", "report": "spending", "date_from": month.isoformat(), "date_to": today.isoformat(),
         "compare_from": last_month.isoformat(), "compare_to": (month - timedelta(days=1)).isoformat()},
    )
    charts = []
    for args in requests:
        result = make_chart(person, args)
        assert result.ok, result.text
        charts.extend(result.charts)
    return charts


def _answer(conversation, charts):
    """Answer the pending turn as the chat runner would store it."""
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    turn.status = AiConversationMessage.Status.DONE
    turn.content = "Here is the chart."
    turn.charts = charts
    turn.save(update_fields=("status", "content", "charts"))


def _check(page, scope, charts):
    expect(page.locator(f"{scope} [data-chat-chart]")).to_have_count(len(charts))
    page.wait_for_function(
        "(scope) => Array.from(document.querySelectorAll(scope + ' [data-chat-chart] canvas'))"
        ".every((c) => window.Chart && window.Chart.getChart(c))", arg=scope,
    )
    layout = page.evaluate(LAYOUT_JS, scope)
    assert [item["kind"] for item in layout] == [chart["kind"] for chart in charts]
    for item, chart in zip(layout, charts, strict=True):
        assert item | {"kind": None} == {
            "drawn": True, "kind": None, "sized": True, "inside": True, "themed": True,
            "tableRows": len(chart["labels"]), "labels": len(chart["labels"]), "link": chart["page_url"],
            "pageScroll": False,
        }, chart["kind"]
    assert page.evaluate("() => window.__cspViolations") == []
    return page.evaluate(MARKUP_JS, scope)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [(390, 844), (1440, 900)], ids=["phone", "desktop"])
def test_chat_page_draws_every_chart_kind_live_and_saved(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        page.add_init_script(CSP_WATCH)
        person = _member(url)
        charts = _charts(person)
        # One pending turn at a time: the live server shares one SQLite connection between its threads.
        # A reply holds at most three charts, so the four kinds arrive over two replies.
        conversation = send_message(person, "Chart my spending")
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        _answer(conversation, charts[:2])
        live = _check(page, "main", charts[:2])
        send_message(person, "And my budgets", conversation_id=conversation.pk)
        page.reload()
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        _answer(conversation, charts[2:])
        live += _check(page, "main", charts)[2:]
        if SCREEN_DIR:
            page.locator("main").screenshot(path=str(Path(SCREEN_DIR) / f"chat-charts-{viewport[0]}-{scheme}.png"))
        page.reload()
        page.wait_for_load_state("networkidle")
        assert _check(page, "main", charts) == live
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_popover_draws_every_chart_kind_live_and_saved(page_session, harness, scheme):  # noqa: F811
    # The popover is a desktop surface (>= 1024px); phones open the chat page instead.
    _state, url = harness
    context, page = page_session((1280, 800), scheme, "/transactions/")
    try:
        page.add_init_script(CSP_WATCH)
        person = _member(url)
        charts = _charts(person)
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("() => typeof window.Chart") == "undefined"  # loaded only once a chart appears
        page.evaluate("() => window.AiPopover.open({origin: document.querySelector('main h1')})")
        expect(page.locator("#ai-popover")).to_be_visible()
        live = []
        for question, shown in (("Chart my spending", charts[:2]), ("And my budgets", charts)):
            page.locator("#ai-popover textarea").fill(question)
            page.locator("#ai-popover-form").get_by_role("button", name="Send").click()
            expect(page.locator("#ai-popover [data-chat-turn-url]")).to_have_count(1)
            conversation_id = page.evaluate("() => JSON.parse(sessionStorage.getItem('finance.ai-popover')).conversationId")
            _answer(AiConversation.objects.get(pk=conversation_id), shown[len(live):])
            live += _check(page, "#ai-popover", shown)[len(live):]
        thread = page.locator("#ai-popover [data-pop-scroll]").evaluate("(el) => el.scrollWidth > el.clientWidth + 1")
        assert thread is False
        page.reload()
        page.wait_for_load_state("networkidle")
        assert _check(page, "#ai-popover", charts) == live
    finally:
        context.close()
