"""Opt-in notices: only newly created, still-visible unread alert kinds leave the app."""

import logging
from contextvars import ContextVar
from functools import wraps

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from .models import Alert, AlertSettings

logger = logging.getLogger(__name__)
_run_alert_ids = ContextVar("email_notice_alert_ids", default=None)


def email_notices_available():
    return bool(settings.EMAIL_HOST and settings.DEFAULT_FROM_EMAIL and settings.ALERT_EMAIL_BASE_URL)


def collect_new_alert(alert):
    ids = _run_alert_ids.get()
    if ids is not None:
        ids.append(alert.pk)


def _send_notice(address, body):
    try:
        sent = send_mail(
            "Financial Planner alerts", body, settings.DEFAULT_FROM_EMAIL, [address],
            fail_silently=False,
        )
        if sent:
            return True
    except Exception:
        # SMTP exceptions can contain recipients, credentials and message content.
        # Never log the exception or its traceback.
        pass
    logger.error("Alert email delivery failed.")
    return False


def send_test_notice(prefs):
    if not email_notices_available() or not prefs.email_enabled or not prefs.notification_email:
        return False
    return _send_notice(
        prefs.notification_email,
        "This is a test notice from Financial Planner.\n\n"
        f"Open your alerts: {settings.ALERT_EMAIL_BASE_URL.rstrip('/')}/alerts/\n",
    )


def send_run_notices(alert_ids):
    from .alert_services import alerts_for

    if not email_notices_available() or not alert_ids:
        return
    recipients = Alert.objects.filter(pk__in=alert_ids).values("recipient_id")
    preferences = AlertSettings.objects.filter(
        person_id__in=recipients, email_enabled=True,
    ).exclude(notification_email="").select_related("person")
    for prefs in preferences:
        rows = list(alerts_for(prefs.person).filter(pk__in=alert_ids, read_at__isnull=True))
        if not rows:
            continue
        labels = sorted({Alert.Kind(row.kind).label for row in rows})
        noun = "alert" if len(rows) == 1 else "alerts"
        body = (
            f"You have {len(rows)} new {noun} in Financial Planner: {', '.join(labels)}.\n\n"
            f"Open your alerts: {settings.ALERT_EMAIL_BASE_URL.rstrip('/')}/alerts/\n"
        )
        _send_notice(prefs.notification_email, body)


def notify_after_alert_run(function):
    """Nested sync/daily passes share a batch; delivery waits for DB commit."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _run_alert_ids.get() is not None:
            return function(*args, **kwargs)
        ids = []
        token = _run_alert_ids.set(ids)
        try:
            result = function(*args, **kwargs)
        finally:
            _run_alert_ids.reset(token)
            transaction.on_commit(lambda: send_run_notices(ids))
        return result

    return wrapped
