"""WebAuthn passkeys as an optional second factor after password sign-in.

Uses Duo Labs `webauthn` (py_webauthn). django-allauth's MFA extra is not
enabled: sign-in and re-auth are custom views, and TOTP is out of scope.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .auth_services import InvalidOneTimeCode, consume_recovery_code
from .models import Passkey, Person
from .security_services import EVENT_TYPES, record_security_event


PENDING_USER_KEY = "passkey_pending_user_id"
PENDING_NEXT_KEY = "passkey_pending_next"
REG_CHALLENGE_KEY = "webauthn_reg_challenge"
AUTH_CHALLENGE_KEY = "webauthn_auth_challenge"

RP_NAME = "Financial Planner"
DEFAULT_PASSKEY_NAME = "Passkey"
MAX_PASSKEY_NAME = 80
LOCAL_DEV_ORIGINS = ("http://localhost", "http://127.0.0.1", "http://testserver")


class PasskeyError(ValueError):
    pass


def relying_party_id():
    """MagicDNS host: first concrete entry of DJANGO_ALLOWED_HOSTS, no port."""
    for host in settings.ALLOWED_HOSTS:
        name = (host or "").strip()
        if not name or name == "*" or name.startswith("."):
            continue
        return name.split(":")[0]
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must include a hostname for passkeys.")


def expected_origins():
    """Origins allowed in WebAuthn clientData, from DJANGO_CSRF_TRUSTED_ORIGINS."""
    origins = [origin.strip().rstrip("/") for origin in settings.CSRF_TRUSTED_ORIGINS if origin.strip()]
    if origins:
        return origins
    if relying_party_id() in {"localhost", "127.0.0.1"}:
        return list(LOCAL_DEV_ORIGINS)
    raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must include the HTTPS origin for passkeys.")


def user_handle_for(person):
    return int(person.pk).to_bytes(8, "big")


def passkeys_for(principal):
    return Passkey.objects.visible_to(principal).order_by("-created_at", "-pk")


def passkey_required_after_password(user):
    person = getattr(user, "person", None)
    if person is None or not person.require_passkey_after_password:
        return False
    return passkeys_for(person).exists()


def store_pending_passkey_login(request, user, next_url):
    request.session[PENDING_USER_KEY] = user.pk
    request.session[PENDING_NEXT_KEY] = next_url or ""
    request.session.pop(AUTH_CHALLENGE_KEY, None)


def pending_passkey_user(request):
    user_id = request.session.get(PENDING_USER_KEY)
    if not user_id:
        return None
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).select_related("person").first()


def pending_passkey_next(request):
    return request.session.get(PENDING_NEXT_KEY) or ""


def clear_pending_passkey_login(request):
    request.session.pop(PENDING_USER_KEY, None)
    request.session.pop(PENDING_NEXT_KEY, None)
    request.session.pop(AUTH_CHALLENGE_KEY, None)


def _normalized_name(name):
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return DEFAULT_PASSKEY_NAME
    return cleaned[:MAX_PASSKEY_NAME]


def _exclude_credentials(person):
    return [
        PublicKeyCredentialDescriptor(id=row.credential_id)
        for row in passkeys_for(person)
    ]


def registration_options_json(request, person):
    options = generate_registration_options(
        rp_id=relying_party_id(),
        rp_name=RP_NAME,
        user_name=person.user.username,
        user_id=user_handle_for(person),
        user_display_name=person.display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_exclude_credentials(person),
    )
    request.session[REG_CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def authentication_options_json(request, person):
    options = generate_authentication_options(
        rp_id=relying_party_id(),
        allow_credentials=_exclude_credentials(person),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    request.session[AUTH_CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def _expected_challenge(request, session_key):
    raw = request.session.pop(session_key, None)
    if not raw:
        raise PasskeyError("Passkey challenge is missing or expired.")
    return base64url_to_bytes(raw)


@transaction.atomic
def register_passkey(request, person, credential, name=""):
    challenge = _expected_challenge(request, REG_CHALLENGE_KEY)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=relying_party_id(),
            expected_origin=expected_origins(),
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as exc:
        raise PasskeyError("Passkey registration failed.") from exc
    if Passkey.objects.filter(credential_id=verified.credential_id).exists():
        raise PasskeyError("Passkey registration failed.")
    passkey = Passkey.objects.create(
        member=person,
        name=_normalized_name(name),
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
    )
    record_security_event(person, EVENT_TYPES.PASSKEY_ADDED, request=request)
    return passkey


def _passkey_for_assertion(person, credential):
    credential_id = None
    if isinstance(credential, dict):
        raw_id = credential.get("rawId") or credential.get("id")
        if raw_id:
            credential_id = base64url_to_bytes(raw_id)
    if credential_id is None:
        raise PasskeyError("Passkey verification failed.")
    passkey = passkeys_for(person).filter(credential_id=credential_id).select_for_update().first()
    if passkey is None:
        raise PasskeyError("Passkey verification failed.")
    return passkey


@transaction.atomic
def assert_passkey(request, person, credential):
    challenge = _expected_challenge(request, AUTH_CHALLENGE_KEY)
    passkey = _passkey_for_assertion(person, credential)
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=relying_party_id(),
            expected_origin=expected_origins(),
            credential_public_key=bytes(passkey.public_key),
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        raise PasskeyError("Passkey verification failed.") from exc
    if verified.credential_id != bytes(passkey.credential_id):
        raise PasskeyError("Passkey verification failed.")
    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = timezone.now()
    passkey.save(update_fields=("sign_count", "last_used_at"))
    return passkey


@transaction.atomic
def complete_login_with_recovery_code(user, code, request=None):
    consume_recovery_code(user, code)
    record_security_event(user, EVENT_TYPES.RECOVERY_CODE_USED, request=request)


@transaction.atomic
def delete_passkey(principal, passkey_id):
    person = principal if isinstance(principal, Person) else getattr(principal, "person", None)
    passkey = passkeys_for(person).filter(pk=passkey_id).first()
    if passkey is None:
        return None
    passkey.delete()
    if not passkeys_for(person).exists() and person.require_passkey_after_password:
        person.require_passkey_after_password = False
        person.save(update_fields=("require_passkey_after_password", "updated_at"))
    record_security_event(person, EVENT_TYPES.PASSKEY_REMOVED)
    return passkey


def set_require_passkey_after_password(person, enabled):
    if enabled and not passkeys_for(person).exists():
        raise PasskeyError("Add a passkey before requiring one after password.")
    if person.require_passkey_after_password == bool(enabled):
        return person
    person.require_passkey_after_password = bool(enabled)
    person.save(update_fields=("require_passkey_after_password", "updated_at"))
    return person
