import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Household, Invitation, LoginThrottle, Membership, Person, RecoveryCode


class InvalidOneTimeCode(ValueError):
    pass


def _digest(value):
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def create_recovery_codes(user, count=8):
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(count)]
    RecoveryCode.objects.bulk_create(
        RecoveryCode(user=user, code_digest=_digest(code)) for code in codes
    )
    return codes


def create_invitation(inviter):
    membership = Membership.objects.filter(
        person=inviter,
        ended_at__isnull=True,
    ).select_related("household").first()
    if membership is None:
        raise PermissionDenied("A current household membership is required.")
    code = secrets.token_urlsafe(24)
    Invitation.objects.create(
        household=membership.household,
        invited_by=inviter,
        token_digest=_digest(code),
        expires_at=timezone.now() + timedelta(hours=settings.INVITATION_TTL_HOURS),
    )
    return code


@transaction.atomic
def accept_invitation(code, username, display_name, password):
    now = timezone.now()
    invitation = Invitation.objects.select_for_update().filter(
        token_digest=_digest(code.strip()),
        used_at__isnull=True,
        expires_at__gt=now,
    ).first()
    if invitation is None:
        raise InvalidOneTimeCode
    try:
        user = get_user_model().objects.create_user(username=username, password=password)
    except IntegrityError as exc:
        raise InvalidOneTimeCode from exc
    person = Person.objects.create(user=user, display_name=display_name)
    Membership.objects.create(person=person, household=invitation.household)
    invitation.used_at = now
    invitation.save(update_fields=("used_at",))
    return user, create_recovery_codes(user)


def revoke_user_sessions(user):
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
            session.delete()


@transaction.atomic
def recover_account(username, code, password):
    user = get_user_model().objects.filter(username__iexact=username).first()
    if user is None:
        raise InvalidOneTimeCode
    recovery_code = RecoveryCode.objects.select_for_update().filter(
        user=user,
        code_digest=_digest(code.strip().lower()),
        used_at__isnull=True,
    ).first()
    if recovery_code is None:
        raise InvalidOneTimeCode
    recovery_code.used_at = timezone.now()
    recovery_code.save(update_fields=("used_at",))
    user.set_password(password)
    user.save(update_fields=("password",))
    revoke_user_sessions(user)
    return user


def throttle_key(username, remote_address):
    return _digest(f"{username.casefold()}\0{remote_address or ''}")


def login_is_blocked(key):
    throttle = LoginThrottle.objects.filter(key_digest=key).first()
    return bool(throttle and throttle.blocked_until and throttle.blocked_until > timezone.now())


@transaction.atomic
def record_login_failure(key):
    now = timezone.now()
    window = timedelta(seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS)
    throttle, _ = LoginThrottle.objects.select_for_update().get_or_create(
        key_digest=key,
        defaults={"window_started_at": now},
    )
    if now - throttle.window_started_at >= window:
        throttle.failure_count = 0
        throttle.window_started_at = now
        throttle.blocked_until = None
    throttle.failure_count += 1
    if throttle.failure_count >= settings.LOGIN_FAILURE_LIMIT:
        throttle.blocked_until = now + timedelta(seconds=settings.LOGIN_BLOCK_SECONDS)
    throttle.save(update_fields=("failure_count", "window_started_at", "blocked_until"))


def clear_login_failures(key):
    LoginThrottle.objects.filter(key_digest=key).delete()


@transaction.atomic
def seed_first_household(username, display_name, household_name, password):
    if Person.objects.exists():
        raise ValueError("The first household member has already been created.")
    user = get_user_model().objects.create_user(username=username, password=password)
    person = Person.objects.create(user=user, display_name=display_name)
    household = Household.objects.create(name=household_name)
    Membership.objects.create(person=person, household=household)
    return user, create_recovery_codes(user)
