"""Break-glass sign-in for production mode (#792).

With GOOGLE_ONLY_SIGN_IN on, no password is accepted anywhere. A member who
cannot sign in with Google gets back in through a one-time link that
`manage.py break_glass_sign_in` prints on the host. Only a keyed digest of the
link's token is stored, the link expires after BREAK_GLASS_TTL, works once,
and a newer link for the same member replaces the earlier one.
"""

import secrets
from datetime import timedelta

from allauth.socialaccount.models import SocialAccount
from django.db import transaction
from django.utils import timezone

from .auth_services import _digest, throttle_key
from .google_auth import _audit_sign_in_change, lock_member_for_sign_in_change
from .models import AuditEvent, BreakGlassGrant
from .security_services import EVENT_TYPES, record_security_event

BREAK_GLASS_TTL = timedelta(minutes=15)
BREAK_GLASS_THROTTLE_USERNAME = "\0break-glass"


def break_glass_throttle_key(remote_address):
    return throttle_key(BREAK_GLASS_THROTTLE_USERNAME, remote_address, per_username=False)


def _token_digest(token):
    return _digest(f"break-glass:{token or ''}")


@transaction.atomic
def issue_break_glass_token(user, *, unlink_google=False):
    """Return a new one-time token for the member, replacing any earlier one."""
    locked = lock_member_for_sign_in_change(user)
    BreakGlassGrant.objects.filter(user=locked).delete()
    token = secrets.token_urlsafe(32)
    BreakGlassGrant.objects.create(
        user=locked, token_digest=_token_digest(token), expires_at=timezone.now() + BREAK_GLASS_TTL
    )
    if unlink_google and SocialAccount.objects.filter(user=locked, provider="google").exists():
        SocialAccount.objects.filter(user=locked, provider="google").delete()
        _audit_sign_in_change(locked, AuditEvent.Action.GOOGLE_DISCONNECTED)
    record_security_event(locked, EVENT_TYPES.BREAK_GLASS_ISSUED)
    return token


def _usable_grants(token):
    return BreakGlassGrant.objects.filter(
        token_digest=_token_digest(token), used_at__isnull=True, expires_at__gt=timezone.now()
    )


def break_glass_token_is_usable(token):
    return _usable_grants(token).exists()


@transaction.atomic
def redeem_break_glass_token(token):
    """Use the token up and return its member, or None when it is not usable."""
    grant = _usable_grants(token).select_for_update().select_related("user").first()
    if grant is None or not hasattr(grant.user, "person"):
        return None
    grant.used_at = timezone.now()
    grant.save(update_fields=("used_at",))
    return grant.user
