from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.apps import AppConfig
from django.utils import timezone


def set_absolute_session_expiry(sender, request, user, **kwargs):
    if request is None:
        return
    request.session.set_expiry(timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))
    from .security_services import EVENT_TYPES, record_security_event, stamp_session_auth_at, touch_member_session

    stamp_session_auth_at(request.session)
    record_security_event(user, EVENT_TYPES.SIGN_IN_SUCCESS, request=request)
    touch_member_session(request, force=True)


def record_sign_out_event(sender, request, user, **kwargs):
    from .security_services import EVENT_TYPES, drop_session_index, record_security_event

    record_security_event(user, EVENT_TYPES.SIGN_OUT, request=request)
    if request is not None:
        drop_session_index(request.session.session_key)


def record_google_connected(sender, request, sociallogin, **kwargs):
    # Only an explicit connect from account settings; onboarding links Google
    # as part of creating the member and is covered by its own events.
    if sociallogin.state.get("process") != "connect":
        return
    from django.db import transaction

    from .audit_services import append_event
    from .models import AuditEvent, Person

    person = Person.objects.filter(user_id=sociallogin.user.pk).first()
    if person is not None:
        with transaction.atomic():
            append_event(action=AuditEvent.Action.GOOGLE_CONNECTED, actor=person, target_id=person.pk)


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "finance"

    def ready(self):
        user_logged_in.connect(set_absolute_session_expiry)
        user_logged_out.connect(record_sign_out_event)

        from allauth.socialaccount.signals import social_account_added

        social_account_added.connect(record_google_connected, dispatch_uid="audit_google_connected")

        from django.db.models.signals import post_delete

        from .models import Receipt
        from .receipt_services import mark_receipt_file_deleted

        # Runs for cascaded deletes too (transactions, accounts, members).
        post_delete.connect(
            lambda sender, instance, **kwargs: mark_receipt_file_deleted(instance.stored_name),
            sender=Receipt,
            weak=False,
            dispatch_uid="receipt_file_deleted",
        )
