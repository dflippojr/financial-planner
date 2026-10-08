"""Shared person lookup and response mapping; authorization stays in services."""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import Person, _person_for

DENIED = "Operation is not permitted."


def service_or_404(action, *, also=()):
    """Map service denials and explicitly selected exceptions to an empty 404."""
    try:
        return action()
    except (PermissionDenied, *also) as exc:
        raise Http404 from exc


def request_person(request, *, missing_ok=False, related=False):
    """Resolve the request member using the caller's existing missing-member policy."""
    if related:
        person = getattr(request.user, "person", None)
        if person is None:
            raise Http404()
        return person
    if missing_ok:
        return Person.objects.filter(user=request.user).first()
    return get_object_or_404(Person, user=request.user)


def require_person(principal, *, check_authenticated=True):
    """Require a member, retaining legacy direct-relation lookup where selected."""
    if check_authenticated:
        person = _person_for(principal)
        if person is None:
            raise PermissionDenied(DENIED)
        return person
    if isinstance(principal, Person):
        return principal
    try:
        return principal.person
    except Person.DoesNotExist as exc:
        raise PermissionDenied(DENIED) from exc


def first_message(exc, fallback, *, stringify=False):
    """Return the first validation message, or the caller's existing fallback."""
    messages = getattr(exc, "messages", None)
    if not messages:
        return fallback
    return str(messages[0]) if stringify else messages[0]
