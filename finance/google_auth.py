from django.conf import settings
from django.contrib.auth import get_user_model

from allauth.socialaccount.models import SocialAccount

from .auth_services import throttle_key


GOOGLE_THROTTLE_USERNAME = "\0google"
GOOGLE_PENDING_SESSION_KEY = "google_pending"
GOOGLE_FAILED = "Google sign-in failed. Try again later, or use a password if you have one."


def google_signin_enabled():
    return bool(
        (getattr(settings, "GOOGLE_CLIENT_ID", "") or "").strip()
        and (getattr(settings, "GOOGLE_CLIENT_SECRET", "") or "").strip()
    )


def google_throttle_key(remote_address):
    return throttle_key(GOOGLE_THROTTLE_USERNAME, remote_address)


def google_email_is_verified(extra_data):
    extra_data = extra_data or {}
    verified = extra_data.get("email_verified")
    if verified is True or verified == "true":
        return True
    verified_email = extra_data.get("verified_email")
    return verified_email is True or verified_email == "true"


def has_google_account(user):
    if user is None or not getattr(user, "pk", None):
        return False
    return SocialAccount.objects.filter(user=user, provider="google").exists()


def sign_in_method_count(user):
    count = 0
    if user.has_usable_password():
        count += 1
    if has_google_account(user):
        count += 1
    return count


def google_uid(extra_data):
    extra_data = extra_data or {}
    return extra_data.get("sub") or extra_data.get("id")


def username_is_taken(username):
    return get_user_model().objects.filter(username=username).exists()
