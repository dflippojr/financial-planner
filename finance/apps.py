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


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "finance"

    def ready(self):
        user_logged_in.connect(set_absolute_session_expiry)
        user_logged_out.connect(record_sign_out_event)
