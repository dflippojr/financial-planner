"""Break-glass sign-in (#792).

A member who cannot sign in gets back in through a one-time link that
`manage.py break_glass_sign_in` prints on the host, with production mode
(GOOGLE_ONLY_SIGN_IN) on or off. Only a keyed digest of the link's token is
stored, the link expires after BREAK_GLASS_TTL, works once, and a newer link
for the same member replaces the earlier one. Issuing a link and using it
each end the member's earlier sessions and unfinished sign-ins.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .auth_services import revoke_user_sessions, throttle_key
from .google_auth import lock_member_for_sign_in_change, unlink_google_for_break_glass
from .models import BreakGlassGrant, Person
from .security_services import EVENT_TYPES, record_security_event

BREAK_GLASS_TTL = timedelta(minutes=15)
BREAK_GLASS_THROTTLE_USERNAME = "\0break-glass"


def break_glass_throttle_key(remote_address):
    return throttle_key(BREAK_GLASS_THROTTLE_USERNAME, remote_address, per_username=False)


def _token_digest(token):
    return hmac.new(settings.SECRET_KEY.encode(), f"break-glass:{token or ''}".encode(), hashlib.sha256).hexdigest()


def _end_member_sessions(locked_user):
    """Sign the locked member out everywhere and drop their unfinished sign-ins.

    The cutoff also ends a session that is not in the session index. A session
    started after this call is stamped later than the cutoff and stays valid.
    """
    Person.objects.filter(user_id=locked_user.pk).update(sessions_valid_after=timezone.now())
    revoke_user_sessions(locked_user)


@transaction.atomic
def issue_break_glass_token(user, *, unlink_google=False):
    """Return a new one-time token for the member, replacing any earlier one.

    In the same transaction the member is signed out everywhere.
    """
    locked = lock_member_for_sign_in_change(user)
    BreakGlassGrant.objects.filter(user=locked).delete()
    token = secrets.token_urlsafe(32)
    BreakGlassGrant.objects.create(
        user=locked, token_digest=_token_digest(token), expires_at=timezone.now() + BREAK_GLASS_TTL
    )
    if unlink_google:
        unlink_google_for_break_glass(locked)
    _end_member_sessions(locked)
    record_security_event(locked, EVENT_TYPES.BREAK_GLASS_ISSUED)
    return token


def _usable_grants(token):
    return BreakGlassGrant.objects.filter(
        token_digest=_token_digest(token), used_at__isnull=True, expires_at__gt=timezone.now()
    )


def break_glass_token_is_usable(token):
    return _usable_grants(token).exists()


def locked_grant_for_redeeming(token):
    # Lock the grant only. Issuing locks the member and then deletes the grant,
    # so locking the joined member here as well could deadlock with it.
    return _usable_grants(token).select_for_update(of=("self",)).select_related("user")


@transaction.atomic
def redeem_break_glass_token(token):
    """Use the token up and return its member, or None when it is not usable.

    In the same transaction the member is signed out everywhere, so the session
    the caller starts next is their only one.
    """
    user_id = _usable_grants(token).values_list("user_id", flat=True).first()
    if user_id is None or not Person.objects.filter(user_id=user_id).exists():
        return None
    # Same lock order as issuing: the member first, then the grant.
    locked = lock_member_for_sign_in_change(get_user_model().objects.get(pk=user_id))
    grant = locked_grant_for_redeeming(token).first()
    if grant is None:
        return None
    grant.used_at = timezone.now()
    grant.save(update_fields=("used_at",))
    _end_member_sessions(locked)
    return locked
