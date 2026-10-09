import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .audit_services import append_event
from .models import AuditEvent, Household, Invitation, LoginThrottle, Membership, Person, RecoveryCode


class InvalidOneTimeCode(ValueError):
    pass


def _digest(value):
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def normalize_username(username):
    """The single username-normalization pipeline shared by creation, sign-in,
    and recovery -- every place a username is turned into its canonical form.

    Django's UserManager.create_user() applies its own NFKC
    normalize_username() to whatever we pass it, after any transformation of
    ours, so our result has to already be a fixed point of that call.
    normalize_username() alone is not enough: a character whose NFKC
    decomposition changes case (e.g. U+210C "ℌ" -> "H") can end up stored
    differently than what we computed if we casefold after it. But
    casefold() alone is not enough either -- full case folding can *un*-NFKC
    a precomposed character into a base character plus a combining mark
    (e.g. U+01F0 "ǰ" folds to "j" + U+030C), which create_user()'s own
    normalize_username() call then recomposes back to something other than
    what we computed. Apply NFKC, then casefold, then NFKC again so the
    result is stable under create_user()'s later NFKC pass no matter which
    direction casefold() perturbed it.
    """
    user_model = get_user_model()
    return user_model.normalize_username(user_model.normalize_username(username.strip()).casefold())


def validated_username(username):
    """normalize_username() plus the user model's own username validators.

    Validation has to run on the normalized value: normalization can lengthen
    a username (casefold turns each U+00DF into "ss"), so a raw-input length
    check does not bound what reaches the database column, and create_user()
    never runs field validators itself. Raises ValidationError.
    """
    normalized = normalize_username(username)
    get_user_model()._meta.get_field("username").run_validators(normalized)
    return normalized


def create_recovery_codes(user, count=8):
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(count)]
    RecoveryCode.objects.bulk_create(
        RecoveryCode(user=user, code_digest=_digest(code)) for code in codes
    )
    return codes


@transaction.atomic
def create_invitation(inviter):
    membership = Membership.objects.filter(
        person=inviter,
        ended_at__isnull=True,
    ).select_related("household").first()
    if membership is None:
        raise PermissionDenied("A current household membership is required.")
    code = secrets.token_urlsafe(24)
    invitation = Invitation.objects.create(
        household=membership.household,
        invited_by=inviter,
        token_digest=_digest(code),
        expires_at=timezone.now() + timedelta(hours=settings.INVITATION_TTL_HOURS),
    )
    append_event(action=AuditEvent.Action.INVITATION_CREATED, actor=inviter, household=membership.household,
                 target_id=invitation.pk)
    return code


def _usable_invitations(code, now):
    """Unused, unexpired invitations whose inviter still belongs to the household."""
    inviter_is_current_member = Membership.objects.filter(
        person_id=OuterRef("invited_by_id"),
        household_id=OuterRef("household_id"),
        ended_at__isnull=True,
    )
    return Invitation.objects.filter(
        Exists(inviter_is_current_member),
        token_digest=_digest((code or "").strip()),
        used_at__isnull=True,
        expires_at__gt=now,
    )


def invitation_is_usable(code):
    return _usable_invitations(code, timezone.now()).exists()


def _create_member_user(username, password):
    if password is None:
        return get_user_model().objects.create_user(username=validated_username(username))
    return get_user_model().objects.create_user(username=validated_username(username), password=password)


@transaction.atomic
def accept_invitation(code, username, display_name, password):
    now = timezone.now()
    invitation = _usable_invitations(code, now).select_for_update().first()
    if invitation is None:
        raise InvalidOneTimeCode
    try:
        user = _create_member_user(username, password)
    except IntegrityError as exc:
        raise InvalidOneTimeCode from exc
    person = Person.objects.create(user=user, display_name=display_name)
    Membership.objects.create(person=person, household=invitation.household)
    invitation.used_at = now
    invitation.save(update_fields=("used_at",))
    append_event(action=AuditEvent.Action.INVITATION_ACCEPTED, actor=person, affected_member=person,
                 household=invitation.household, target_id=invitation.pk)
    return user, create_recovery_codes(user)


def revoke_user_sessions(user):
    from .passkey_services import clear_pending_passkey_logins_for_user
    from .security_services import revoke_indexed_sessions_for_user

    revoke_indexed_sessions_for_user(user)
    clear_pending_passkey_logins_for_user(user)
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
            session.delete()


@transaction.atomic
def recover_account(username, code, password):
    # Stored usernames are already normalize_username()'s output, so an
    # exact match on the normalized submission is both correct and more
    # precise than an iexact match against the raw input, which wouldn't
    # apply the same NFKC/casefold pipeline used at creation and sign-in.
    user = get_user_model().objects.filter(username=normalize_username(username)).first()
    if user is None:
        raise InvalidOneTimeCode
    consume_recovery_code(user, code)
    user.set_password(password)
    user.save(update_fields=("password",))
    person = Person.objects.filter(user_id=user.pk).first()
    if person is not None:
        append_event(action=AuditEvent.Action.PASSWORD_CHANGED, actor=person, target_id=person.pk)
    revoke_user_sessions(user)
    return user


@transaction.atomic
def consume_recovery_code(user, code):
    recovery_code = RecoveryCode.objects.select_for_update().filter(
        user=user,
        code_digest=_digest((code or "").strip().lower()),
        used_at__isnull=True,
    ).first()
    if recovery_code is None:
        raise InvalidOneTimeCode
    recovery_code.used_at = timezone.now()
    recovery_code.save(update_fields=("used_at",))
    return recovery_code


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


# Stable PostgreSQL advisory-lock key for first-member creation (issue #53).
FIRST_MEMBER_ADVISORY_LOCK = int.from_bytes(
    hashlib.sha256(b"finance.seed_first_household").digest()[:8],
    "big",
    signed=True,
)

SETUP_THROTTLE_USERNAME = "\0setup"


def first_member_exists():
    return get_user_model().objects.exists() or Person.objects.exists()


def setup_code_configured():
    return bool((getattr(settings, "SETUP_CODE", None) or "").strip())


def setup_code_matches(submitted):
    """Compare a submitted setup code to SETUP_CODE in constant time.

    Both values are HMAC-SHA256 digested so compare_digest always sees equal
    lengths. Callers must not log or echo `submitted` or the configured code.
    """
    expected = (getattr(settings, "SETUP_CODE", None) or "").strip()
    given = (submitted or "").strip()
    key = settings.SECRET_KEY.encode()

    def keyed_digest(value):
        return hmac.new(key, value.encode(), hashlib.sha256).digest()

    return hmac.compare_digest(keyed_digest(given), keyed_digest(expected))


def lock_first_member_creation():
    """Serialize first-member existence check and create on one database.

    PostgreSQL uses a transaction-scoped advisory lock. SQLite has no
    equivalent, so tests that need two concurrent writers skip it.
    """
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [FIRST_MEMBER_ADVISORY_LOCK])
        return
    list(get_user_model().objects.select_for_update())
    list(Person.objects.select_for_update())


def complete_member_session(request, user):
    # Sessions last a fixed period from sign-in. Django's default expiry is
    # relative to the last time the session was saved, so any later write to
    # it (such as CSV staging metadata) would push the expiry out again.
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.set_expiry(timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))
    from .reauth import stamp_recent_auth

    stamp_recent_auth(request)


@transaction.atomic
def seed_first_household(username, display_name, household_name, password):
    lock_first_member_creation()
    if first_member_exists():
        raise ValueError("The first household member has already been created.")
    try:
        user = _create_member_user(username, password)
    except IntegrityError as exc:
        raise ValueError("The first household member could not be created.") from exc
    person = Person.objects.create(user=user, display_name=display_name)
    household = Household.objects.create(name=household_name)
    Membership.objects.create(person=person, household=household)
    append_event(action=AuditEvent.Action.HOUSEHOLD_CREATED, actor=person, affected_member=person,
                 household=household, target_id=household.pk)
    from .category_services import ensure_household_categories

    ensure_household_categories(household)
    return user, create_recovery_codes(user)
