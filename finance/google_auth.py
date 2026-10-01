import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from allauth.socialaccount.models import SocialAccount

from .auth_services import throttle_key
from .lifecycle_services import lock_actor_household
from .models import Person


GOOGLE_THROTTLE_USERNAME = "\0google"
GOOGLE_PENDING_SESSION_KEY = "google_pending"
GOOGLE_FLOW_BIND_KEY = "google_pending_bind"
GOOGLE_FLOW_NONCE_STATE_KEY = "google_flow_nonce"
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


def _pending_map(session_value):
    if not isinstance(session_value, dict):
        return {}
    if session_value.get("intent") in ("join", "setup", "login"):
        return {}
    return dict(session_value)


def store_google_pending(request, payload):
    nonce = secrets.token_urlsafe(16)
    pending = _pending_map(request.session.get(GOOGLE_PENDING_SESSION_KEY))
    pending[nonce] = payload
    request.session[GOOGLE_PENDING_SESSION_KEY] = pending
    request.session[GOOGLE_FLOW_BIND_KEY] = nonce
    return nonce


def peek_google_pending(request, nonce):
    if not nonce:
        return None
    payload = _pending_map(request.session.get(GOOGLE_PENDING_SESSION_KEY)).get(nonce)
    if not isinstance(payload, dict):
        return None
    return payload


def consume_google_pending(request, sociallogin):
    nonce = (sociallogin.state or {}).get(GOOGLE_FLOW_NONCE_STATE_KEY)
    payload = peek_google_pending(request, nonce)
    if not nonce:
        return None
    pending = _pending_map(request.session.get(GOOGLE_PENDING_SESSION_KEY))
    pending.pop(nonce, None)
    request.session[GOOGLE_PENDING_SESSION_KEY] = pending
    return payload


def bind_google_flow_nonce(request, state):
    nonce = request.session.pop(GOOGLE_FLOW_BIND_KEY, None)
    if not nonce:
        return None
    state[GOOGLE_FLOW_NONCE_STATE_KEY] = nonce
    return nonce


def lock_member_for_sign_in_change(user):
    locked = get_user_model().objects.select_for_update().get(pk=user.pk)
    person = Person.objects.filter(user_id=locked.pk).first()
    if person is not None:
        lock_actor_household(person)
    return locked


@transaction.atomic
def disconnect_google_account(user):
    locked = lock_member_for_sign_in_change(user)
    if sign_in_method_count(locked) <= 1:
        return False
    SocialAccount.objects.filter(user=locked, provider="google").delete()
    return True


@transaction.atomic
def remove_member_password(user):
    locked = lock_member_for_sign_in_change(user)
    if not has_google_account(locked) or not locked.has_usable_password():
        return False
    locked.set_unusable_password()
    locked.save(update_fields=("password",))
    return True
