"""Opt-in notices: only pending, still-visible unread alert kinds leave the app."""

import logging
from contextvars import ContextVar
from functools import wraps

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Alert, AlertSettings
from .audit_operations import outcome

logger = logging.getLogger(__name__)
_notice_run_active = ContextVar("email_notice_run_active", default=False)


def email_notices_available():
    return bool(settings.EMAIL_HOST and settings.DEFAULT_FROM_EMAIL and settings.ALERT_EMAIL_BASE_URL)


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


def send_run_notices(alert_ids=None):
    from .alert_services import alerts_for

    if not email_notices_available():
        return
    pending = Alert.objects.filter(read_at__isnull=True, email_notice_sent_at__isnull=True)
    if alert_ids is not None:
        pending = pending.filter(pk__in=alert_ids)
    recipients = pending.values("recipient_id")
    preference_ids = list(AlertSettings.objects.filter(
        person_id__in=recipients, email_enabled=True,
    ).exclude(notification_email="").order_by("pk").values_list("pk", flat=True))
    for preference_id in preference_ids:
        _send_member_notice(preference_id, alert_ids, alerts_for)


def _send_member_notice(preference_id, alert_ids, visible_alerts):
    # Serialize overlapping scheduler/manual runs for this member. Recheck
    # preferences and unread/unsent status under the lock before delivery.
    with transaction.atomic():
        prefs = AlertSettings.objects.select_for_update(of=("self",)).select_related("person").get(pk=preference_id)
        if not prefs.email_enabled or not prefs.notification_email:
            return
        rows = visible_alerts(prefs.person).filter(read_at__isnull=True, email_notice_sent_at__isnull=True)
        if alert_ids is not None:
            rows = rows.filter(pk__in=alert_ids)
        rows = list(rows)
        if not rows:
            return
        labels = sorted({Alert.Kind(row.kind).label for row in rows})
        noun = "alert" if len(rows) == 1 else "alerts"
        body = (
            f"You have {len(rows)} new {noun} in Financial Planner: {', '.join(labels)}.\n\n"
            f"Open your alerts: {settings.ALERT_EMAIL_BASE_URL.rstrip('/')}/alerts/\n"
        )
        sent = _send_notice(prefs.notification_email, body)
        outcome(prefs.person, "email_delivery", phase="succeeded" if sent else "failed",
                metadata={"row_count": len(rows)})
        if sent:
            Alert.objects.filter(pk__in=[row.pk for row in rows]).update(email_notice_sent_at=timezone.now())


def notify_after_alert_run(function):
    """Nested sync/daily passes share a batch; delivery waits for DB commit."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _notice_run_active.get():
            return function(*args, **kwargs)
        token = _notice_run_active.set(True)
        try:
            result = function(*args, **kwargs)
        finally:
            _notice_run_active.reset(token)
            transaction.on_commit(send_run_notices)
        return result

    return wrapped
