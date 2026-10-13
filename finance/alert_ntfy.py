"""Opt-in ntfy push notices: recurring price changes and the weekly digest summary.

Unlike email notices, a price-change push names the series and its old and new
price (issue #420). It never carries account names, numbers or balances. Logs
record only success or failure: never the token, the topic or the message.
"""

import logging
import re
import urllib.request

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .audit_operations import member_operation, outcome, scheduled_operation
from .models import NTFY_TOPIC_PATTERN, Alert, AlertSettings

logger = logging.getLogger(__name__)
_TOPIC = re.compile(NTFY_TOPIC_PATTERN)
MAX_PRICE_LINES = 10
# ntfy turns a body over 4,096 bytes into an attachment; leave room for the link lines.
MAX_PRICE_BYTES = 3_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they would carry the bearer token to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """POST one message to NTFY_URL/<topic>; a single attempt, no retries."""

    opener = urllib.request.build_opener(_NoRedirect)

    def send(self, topic, title, body, click_url, tags=""):
        headers = {"Title": title, "Content-Type": "text/plain; charset=utf-8"}
        if tags:
            headers["Tags"] = tags
        if click_url:
            headers["Click"] = click_url
        if settings.NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {settings.NTFY_TOKEN}"
        request = urllib.request.Request(
            f"{settings.NTFY_URL}/{topic}", data=body.encode("utf-8"), headers=headers, method="POST",
        )
        with self.opener.open(request, timeout=settings.NTFY_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300


class FakeTransport:
    """Records messages in memory. Tests and disposable UI instances use only this."""

    def __init__(self):
        self.messages = []

    def send(self, topic, title, body, click_url, tags=""):
        self.messages.append({"topic": topic, "title": title, "body": body, "click": click_url, "tags": tags})
        return True


fake_transport = FakeTransport()
_http_transport = HttpTransport()


def get_transport():
    return fake_transport if settings.NTFY_TRANSPORT == "fake" else _http_transport


def ntfy_notices_available():
    return settings.NTFY_URL.startswith(("http://", "https://"))


def valid_topic(topic):
    return bool(topic and _TOPIC.match(topic))


def _absolute(path):
    base = settings.ALERT_EMAIL_BASE_URL.rstrip("/")
    return f"{base}{path}" if base else ""


def _send(topic, title, body, click_url, tags):
    if not valid_topic(topic):
        return False
    try:
        if get_transport().send(topic, title, body, click_url, tags):
            return True
    except Exception:
        # HTTP errors can quote the URL (with the topic) or request headers.
        # Never log the exception or its traceback.
        pass
    logger.error("Alert ntfy delivery failed.")
    return False


def _link_line(label, url):
    return f"\n\n{label}: {url}" if url else ""


@member_operation
def send_test_ntfy(prefs):
    if not ntfy_notices_available() or not prefs.ntfy_enabled or not valid_topic(prefs.ntfy_topic):
        return False
    alerts_url = _absolute(reverse("alert-list"))
    sent = _send(
        prefs.ntfy_topic, "Financial Planner test",
        "This is a test notice from Financial Planner." + _link_line("Open your alerts", alerts_url),
        alerts_url, "white_check_mark",
    )
    outcome(prefs.person, "ntfy_delivery", phase="succeeded" if sent else "failed")
    return sent


@scheduled_operation
def send_run_ntfy():
    """One price-change push and at most one weekly digest push per opted-in member."""
    if not ntfy_notices_available():
        return
    preference_ids = list(
        AlertSettings.objects.filter(ntfy_enabled=True).exclude(ntfy_topic="").order_by("pk").values_list("pk", flat=True)
    )
    for preference_id in preference_ids:
        try:
            _send_member_ntfy(preference_id)
        except Exception:
            # A failed member must not break the alert run, the request, or the next member.
            logger.error("Alert ntfy delivery failed.")


def _price_message(rows):
    """Title, body and link for the rows that fit; the rest wait for the next push."""
    included, size = [], 0
    for row in rows[:MAX_PRICE_LINES]:
        size += len(row.title.encode("utf-8")) + 1
        if included and size > MAX_PRICE_BYTES:
            break
        included.append(row)
    lines = [row.title for row in included]
    if len(rows) > len(included):
        lines.append(f"and {len(rows) - len(included)} more")
    review_url = _absolute(reverse("recurring-review"))
    title = "Recurring price change" if len(rows) == 1 else f"{len(rows)} recurring price changes"
    return included, title, "\n".join(lines) + _link_line("Review recurring charges", review_url), review_url


def _plural(count, singular, plural):
    return f"{count} {singular if count == 1 else plural}"


def _digest_message(digest):
    parts = [
        _plural(len(digest.new_series), "new recurring series", "new recurring series"),
        _plural(len(digest.price_changes), "price change", "price changes"),
        _plural(len(digest.large_transactions), "large transaction", "large transactions"),
    ]
    home_url = _absolute(reverse("home") + "?digest_week=last#weekly-digest")
    body = (
        f"Week of {digest.start:%b} {digest.start.day} to {digest.end:%b} {digest.end.day}: "
        f"{', '.join(parts)}." + _link_line("Open the digest", home_url)
    )
    return "Weekly digest", body, home_url


def _send_member_ntfy(preference_id):
    from .alert_services import alerts_for
    from .weekly_digest import WEEK_LAST, build_weekly_digest

    # Serialize overlapping runs for this member; recheck preferences under the lock.
    with transaction.atomic():
        prefs = AlertSettings.objects.select_for_update(of=("self",)).select_related("person").get(pk=preference_id)
        if not prefs.ntfy_enabled or not valid_topic(prefs.ntfy_topic):
            return
        if prefs.recurring_price_enabled:
            rows = list(
                alerts_for(prefs.person)
                .filter(kind=Alert.Kind.RECURRING_PRICE, read_at__isnull=True, ntfy_notice_sent_at__isnull=True)
                .order_by("created_at", "pk")
            )
            if rows:
                included, title, body, click = _price_message(rows)
                sent = _send(prefs.ntfy_topic, title, body, click, "money_with_wings")
                outcome(prefs.person, "ntfy_delivery", phase="succeeded" if sent else "failed",
                        metadata={"row_count": len(included)})
                if sent:
                    Alert.objects.filter(pk__in=[row.pk for row in included]).update(ntfy_notice_sent_at=timezone.now())
        digest = build_weekly_digest(prefs.person, today=timezone.localdate(), week=WEEK_LAST)
        if digest.total and prefs.ntfy_digest_week != digest.start:
            title, body, click = _digest_message(digest)
            sent = _send(prefs.ntfy_topic, title, body, click, "calendar")
            outcome(prefs.person, "ntfy_delivery", phase="succeeded" if sent else "failed")
            if sent:
                prefs.ntfy_digest_week = digest.start
                prefs.save(update_fields=("ntfy_digest_week",))
