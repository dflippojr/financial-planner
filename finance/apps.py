from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.apps import AppConfig
from django.utils import timezone


def set_absolute_session_expiry(sender, request, user, **kwargs):
    if request is None:
        return
    request.session.set_expiry(timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "finance"

    def ready(self):
        user_logged_in.connect(set_absolute_session_expiry)
