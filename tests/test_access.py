from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from finance.access import (
    DENIED,
    first_message,
    request_person,
    require_person,
    service_or_404,
)
from finance.models import Person


@pytest.mark.django_db
def test_person_lookup_variants():
    user = get_user_model().objects.create_user(username="synthetic-member")
    request = SimpleNamespace(user=user)
    assert request_person(request, missing_ok=True) is None
    for options in ({}, {"related": True}):
        with pytest.raises(Http404):
            request_person(request, **options)
    for options in ({}, {"check_authenticated": False}):
        with pytest.raises(PermissionDenied, match=DENIED):
            require_person(user, **options)
    person = Person.objects.create(user=user, display_name="Synthetic Member")
    request.user = get_user_model().objects.get(pk=user.pk)
    for options in ({}, {"missing_ok": True}, {"related": True}):
        assert request_person(request, **options) == person
    for principal in (person, request.user):
        for options in ({}, {"check_authenticated": False}):
            assert require_person(principal, **options) == person


@pytest.mark.parametrize("options, error", [({}, PermissionDenied), ({"check_authenticated": False}, AttributeError)])
def test_anonymous_principal_variants(options, error):
    with pytest.raises(error):
        require_person(AnonymousUser(), **options)
    request = SimpleNamespace(user=AnonymousUser())
    with pytest.raises(Http404):
        request_person(request, related=True)
    with pytest.raises(TypeError):
        request_person(request)
    with pytest.raises(TypeError):
        request_person(request, missing_ok=True)


@pytest.mark.parametrize("error, also, mapped", [(PermissionDenied, (), True), (ValidationError, (), False), (ValidationError, (ValidationError,), True), (ValueError, (ValidationError,), False)])
def test_service_exception_mapping(error, also, mapped):
    exc = error("Synthetic denial")
    def action():
        raise exc
    with pytest.raises(Http404 if mapped else error) as caught:
        service_or_404(action, also=also)
    assert (caught.value.__cause__ is exc and str(caught.value) == "") if mapped else caught.value is exc
    assert service_or_404(lambda: 42, also=also) == 42


@pytest.mark.parametrize("messages, expected", [(None, "Fallback"), ([], "Fallback"), ([42, "later"], 42)])
def test_first_message_variants(messages, expected):
    exc = SimpleNamespace(messages=messages)
    assert first_message(exc, "Fallback") == expected
    assert first_message(exc, "Fallback", stringify=True) == str(expected)
