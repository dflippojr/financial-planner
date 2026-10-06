"""A member's own Claude or Codex plan, linked through Agent Harness end-user logins."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .ai_harness import (
    _label,
    end_user_login_state,
    failure_from_http,
    start_end_user_login,
    submit_end_user_code,
    unlink_end_user_login,
)
from .ai_http import HarnessHttpError
from .ai_types import AUTHORIZATION_REQUIRED, PLAN_BACKENDS, PLAN_REPLACES_API, UNAVAILABLE
from .category_services import current_household
from .encryption import decrypt_secret
from .lifecycle_services import _DENIED, _person_for
from .models import AiPlanLink, AiProviderConnection, Membership
from .policy_services import may_use_ai

LOGIN_EXPIRY_SECONDS = 600
LINK_PROMPT = "Link your plan to use this. Open Settings → AI and choose Link under Your Claude / Codex plan."
NO_HOST = "Ask the household host to turn on plan linking, or connect your own Agent Harness."
LINK_FAILED = "The plan sign-in could not be started. Try again in a moment."
CODE_FAILED = "That code was not accepted. Start the link again to get a new one."
_ENDED_STATES = frozenset({"failed", "expired", "cancelled", "denied"})


class PlanLinkError(Exception):
    pass


def end_user_id(person):
    """A stable opaque id for the harness: an HMAC of the Person id, never a name or email."""
    key = (getattr(settings, "FIELD_ENCRYPTION_KEY", "") or "").encode("utf-8")
    digest = hmac.new(key, f"plan-link:{person.pk}".encode(), hashlib.sha256).hexdigest()
    return f"fp-{digest[:32]}"


def plan_link(person, backend):
    if backend not in PLAN_BACKENDS:
        return None
    return AiPlanLink.objects.filter(person=person, backend=backend).select_related("connection").first()


def plan_links(person):
    return list(AiPlanLink.objects.filter(person=person).select_related("connection").order_by("backend"))


def plan_end_user(person, connection, backend):
    """The end_user to send, only when this connection is the one the member linked through."""
    link = plan_link(person, backend)
    if link is not None and connection is not None and link.connection_id == connection.pk:
        return end_user_id(person)
    return ""


def _own_harness(person):
    return AiProviderConnection.objects.filter(
        owner=person, kind=AiProviderConnection.Kind.AGENT_HARNESS
    ).first()


def offered_plan_connection(person):
    household = current_household(person)
    if household is None:
        return None
    owner_ids = (
        Membership.objects.filter(household=household, ended_at__isnull=True)
        .exclude(person=person)
        .values("person_id")
    )
    return (
        AiProviderConnection.objects.filter(
            owner_id__in=owner_ids,
            kind=AiProviderConnection.Kind.AGENT_HARNESS,
            offer_plan_links=True,
        )
        .order_by("pk")
        .first()
    )


def host_connection(person):
    """The harness a member links through: their own, else the one the household host offers."""
    own = _own_harness(person)
    return own if own is not None else offered_plan_connection(person)


def set_offer_plan_links(principal, offered):
    person = _person_for(principal)
    connection = _own_harness(person)
    if connection is None:
        raise PermissionDenied(_DENIED)
    connection.offer_plan_links = bool(offered)
    connection.save(update_fields=("offer_plan_links",))
    if not offered:
        AiPlanLink.objects.filter(connection=connection).exclude(person=person).delete()
    return connection


def mark_login_required(person, backend):
    AiPlanLink.objects.filter(person=person, backend=backend).update(needs_login=True)


def _context(principal, backend):
    person = _person_for(principal)
    if backend not in PLAN_BACKENDS:
        raise PlanLinkError("Choose Claude or Codex.")
    if not may_use_ai(person):
        raise PlanLinkError("AI is off until the current privacy and data policy is accepted.")
    connection = host_connection(person)
    if connection is None:
        raise PlanLinkError(NO_HOST)
    try:
        token = decrypt_secret(connection.encrypted_token)
    except Exception:
        raise PlanLinkError("This AI connection can no longer be read.") from None
    return person, connection, token


def _safe_url(value):
    parsed = urlparse(str(value or ""))
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return str(value)
    return ""


def _failure(exc, fallback):
    code = failure_from_http(exc)
    if code == AUTHORIZATION_REQUIRED:
        return "Agent Harness did not accept the saved token."
    if code == UNAVAILABLE:
        return "The app can't reach Agent Harness."
    return fallback


def start_login(principal, backend):
    person, connection, token = _context(principal, backend)
    try:
        started = start_end_user_login(connection.base_url, token, end_user_id(person), backend)
    except HarnessHttpError as exc:
        raise PlanLinkError(_failure(exc, LINK_FAILED)) from None
    url = _safe_url(started.get("verification_url"))
    attempt_id = str(started.get("attempt_id") or "")
    if not url or not attempt_id:
        raise PlanLinkError(LINK_FAILED)
    return {
        "attempt_id": attempt_id,
        "verification_url": url,
        "user_code": str(started.get("user_code") or ""),
        "needs_code": bool(started.get("needs_code")),
        "expires_in": LOGIN_EXPIRY_SECONDS,
    }


def submit_code(principal, backend, *, attempt_id, code):
    person, connection, token = _context(principal, backend)
    secret = (code or "").strip()
    if not secret or not attempt_id:
        raise PlanLinkError("Paste the code from the sign-in page.")
    try:
        submit_end_user_code(connection.base_url, token, end_user_id(person), backend, str(attempt_id), secret)
    except HarnessHttpError as exc:
        # Never echo the harness body: it could quote the code.
        raise PlanLinkError(_failure(exc, CODE_FAILED) if exc.status == 0 else CODE_FAILED) from None
    return {"ok": True}


def poll_login(principal, backend):
    person, connection, token = _context(principal, backend)
    try:
        state = end_user_login_state(connection.base_url, token, end_user_id(person), backend)
    except HarnessHttpError as exc:
        raise PlanLinkError(_failure(exc, LINK_FAILED)) from None
    if state.get("linked"):
        AiPlanLink.objects.update_or_create(
            person=person,
            backend=backend,
            defaults={"connection": connection, "needs_login": False, "linked_at": timezone.now()},
        )
        return {"linked": True, "failed": False}
    attempt = state.get("attempt") if isinstance(state.get("attempt"), dict) else {}
    return {"linked": False, "failed": str(attempt.get("status") or "") in _ENDED_STATES}


def unlink(principal, backend):
    person = _person_for(principal)
    link = plan_link(person, backend)
    if link is None:
        return
    try:
        token = decrypt_secret(link.connection.encrypted_token)
        unlink_end_user_login(link.connection.base_url, token, end_user_id(person), backend)
    except HarnessHttpError as exc:
        if exc.status != 404:
            raise PlanLinkError("The plan could not be unlinked. Try again in a moment.") from None
    except Exception:
        raise PlanLinkError("The plan could not be unlinked. Try again in a moment.") from None
    link.delete()


def plan_cards(person):
    links = {link.backend: link for link in plan_links(person)}
    host = host_connection(person)
    key_kinds = set(
        AiProviderConnection.objects.filter(owner=person).values_list("kind", flat=True)
    )
    cards = []
    for backend in PLAN_BACKENDS:
        link = links.get(backend)
        cards.append(
            {
                "backend": backend,
                "label": _label(backend),
                "linked": link is not None,
                "needs_login": bool(link and link.needs_login),
                "has_key": PLAN_REPLACES_API[backend] in key_kinds,
                "available": host is not None,
            }
        )
    return cards
