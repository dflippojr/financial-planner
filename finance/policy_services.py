"""Privacy policy versions, acceptance, and AI-backend guards."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Membership, Person, PrivacyPolicyAcceptance, PrivacyPolicyVersion

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy" / "default.md"
DIRECTORY_FILENAMES = ("privacy-policy.md", "privacy_policy.md", "policy.md")


class PolicySourceError(ValueError):
    pass


def _as_person(member):
    if isinstance(member, Person):
        return member
    person = getattr(member, "person", None)
    if person is None:
        raise TypeError("A household member is required.")
    return person


def policy_source_path():
    configured = (getattr(settings, "PRIVACY_POLICY_PATH", None) or "").strip()
    if not configured:
        return DEFAULT_POLICY_PATH
    path = Path(configured)
    if path.is_dir():
        for name in DIRECTORY_FILENAMES:
            candidate = path / name
            if candidate.is_file():
                return candidate
        raise PolicySourceError(
            "The configured privacy-policy directory has no privacy-policy.md file."
        )
    return path


def load_policy_source():
    path = policy_source_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicySourceError("The privacy policy source could not be read.") from exc


def latest_material_policy():
    return PrivacyPolicyVersion.objects.filter(is_material=True).order_by("-version").first()


def current_policy(*, create_if_missing=True):
    latest = PrivacyPolicyVersion.objects.order_by("-version").first()
    if latest is not None:
        return latest
    if not create_if_missing:
        return None
    return publish_policy(material=True)


def publish_policy(*, material, body=None):
    text = load_policy_source() if body is None else body
    with transaction.atomic():
        last = (
            PrivacyPolicyVersion.objects.select_for_update()
            .order_by("-version")
            .first()
        )
        next_version = 1 if last is None else last.version + 1
        is_material = bool(material) or last is None
        try:
            return PrivacyPolicyVersion.objects.create(
                version=next_version,
                body=text,
                is_material=is_material,
                published_at=timezone.now(),
            )
        except IntegrityError:
            return PrivacyPolicyVersion.objects.order_by("-version").first()


def in_acceptance(member):
    person = _as_person(member)
    material = latest_material_policy()
    if material is None:
        current_policy()
        material = latest_material_policy()
    if material is None:
        return False
    return PrivacyPolicyAcceptance.objects.filter(
        person=person,
        policy_version__version__gte=material.version,
    ).exists()


def may_use_ai(member):
    return in_acceptance(member)


def household_ai_allowed(household):
    member_ids = Membership.objects.filter(
        household=household,
        ended_at__isnull=True,
    ).values_list("person_id", flat=True)
    people = list(Person.objects.filter(pk__in=member_ids))
    if not people:
        return True
    return all(in_acceptance(person) for person in people)


def accept_policy(person, version=None):
    person = _as_person(person)
    policy = version or current_policy()
    PrivacyPolicyAcceptance.objects.get_or_create(
        person=person,
        policy_version=policy,
        defaults={"accepted_at": timezone.now()},
    )
    return policy


def decline_policy(person, version=None):
    person = _as_person(person)
    policy = version or current_policy()
    person.privacy_policy_declined_version = policy
    person.save(update_fields=("privacy_policy_declined_version",))
    return policy


def should_prompt_privacy_policy(person):
    person = _as_person(person)
    if in_acceptance(person):
        return False
    material = latest_material_policy()
    if material is None:
        return True
    declined = person.privacy_policy_declined_version
    if declined is not None and declined.version >= material.version:
        return False
    return True


def latest_acceptance(person):
    person = _as_person(person)
    return (
        PrivacyPolicyAcceptance.objects.filter(person=person)
        .select_related("policy_version")
        .order_by("-policy_version__version", "-accepted_at")
        .first()
    )


def record_onboarding_acceptance(person, accepted):
    if accepted:
        accept_policy(person)
