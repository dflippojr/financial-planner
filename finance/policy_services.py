"""Privacy policy versions, acceptance, and AI-backend guards."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .audit_services import append_event
from .models import AuditEvent, Person, PrivacyPolicyAcceptance, PrivacyPolicyVersion

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
            with transaction.atomic():
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


def household_consent_status(household):
    """Current members' consent only; denials deliberately look unanswered."""
    material = latest_material_policy()
    if material is None:
        current_policy()
        material = latest_material_policy()
    accepted = PrivacyPolicyAcceptance.objects.filter(
        person_id=OuterRef("pk"), policy_version__version__gte=material.version,
    )
    people = Person.objects.filter(
        memberships__household=household, memberships__ended_at__isnull=True,
    ).annotate(policy_accepted=Exists(accepted)).order_by("display_name", "pk")
    members = [
        {"id": person.pk, "name": person.display_name,
         "status": "accepted" if person.policy_accepted else "not_answered"}
        for person in people
    ]
    pending = sum(member["status"] != "accepted" for member in members)
    return {"members": members, "total": len(members), "pending": pending,
            "accepted": len(members) - pending}


def household_ai_allowed(household):
    return household_consent_status(household)["pending"] == 0


def ai_slot_state(person):
    """The sidebar state and the current viewer's household consent summary."""
    from .ai_services import member_has_ai
    from .category_services import current_household

    person = _as_person(person)
    accepted = in_acceptance(person)
    material = latest_material_policy()
    declined = person.privacy_policy_declined_version
    denied = not accepted and declined is not None and declined.version >= material.version
    household = current_household(person)
    summary = household_consent_status(household) if household else {
        "members": [], "total": 0, "pending": 0, "accepted": 0,
    }
    summary["members"].sort(key=lambda member: member["id"] != person.pk)
    for member in summary["members"]:
        member["is_you"] = member["id"] == person.pk
    backend = member_has_ai(person) if accepted else False
    if denied:
        state = "denied"
    elif not accepted:
        state = "undecided"
    elif not backend:
        state = "no_backend"
    elif summary["pending"]:
        state = "household_pending"
    else:
        state = "ready"
    return {"state": state, "in_acceptance": accepted, "has_backend": backend,
            "household": summary}


def accept_policy(person, version, *, request=None):
    if version is None:
        raise TypeError("A shown policy version is required.")
    person = _as_person(person)
    with transaction.atomic():
        _acceptance, created = PrivacyPolicyAcceptance.objects.get_or_create(
            person=person,
            policy_version=version,
            defaults={"accepted_at": timezone.now()},
        )
        person.privacy_policy_declined_version = None
        person.privacy_policy_dismissed_version = None
        person.save(update_fields=("privacy_policy_declined_version", "privacy_policy_dismissed_version"))
        if created:
            append_event(action=AuditEvent.Action.PRIVACY_ACCEPTED, actor=person, target_id=person.pk)
    if created:
        from .security_services import EVENT_TYPES, record_security_event

        record_security_event(person, EVENT_TYPES.POLICY_ACCEPTANCE, request=request)
    return version


def accept_shown_version(person, shown_version, *, request=None):
    try:
        shown = int(shown_version)
    except (TypeError, ValueError):
        return False
    current = current_policy()
    if shown != current.version:
        return False
    accept_policy(person, current, request=request)
    return True


def decline_policy(person, version=None):
    person = _as_person(person)
    policy = version or current_policy()
    changed = person.privacy_policy_declined_version_id != policy.pk
    with transaction.atomic():
        person.privacy_policy_declined_version = policy
        person.save(update_fields=("privacy_policy_declined_version",))
        if changed:
            append_event(action=AuditEvent.Action.PRIVACY_DECLINED, actor=person, target_id=person.pk)
    return policy


def decline_shown_version(person, shown_version):
    try:
        shown = int(shown_version)
    except (TypeError, ValueError):
        return False
    current = current_policy()
    if shown != current.version:
        return False
    decline_policy(person, current)
    return True


def dismiss_shown_version(person, shown_version):
    try:
        shown = int(shown_version)
    except (TypeError, ValueError):
        return False
    policy = current_policy()
    if shown != policy.version:
        return False
    person = _as_person(person)
    person.privacy_policy_dismissed_version = policy
    person.save(update_fields=("privacy_policy_dismissed_version",))
    return True


def should_prompt_privacy_policy(person):
    person = _as_person(person)
    if in_acceptance(person):
        return False
    material = latest_material_policy()
    if material is None:
        return True
    declined = person.privacy_policy_declined_version
    dismissed = person.privacy_policy_dismissed_version
    return not any(
        version is not None and version.version >= material.version
        for version in (declined, dismissed)
    )


def latest_acceptance(person):
    person = _as_person(person)
    return (
        PrivacyPolicyAcceptance.objects.filter(person=person)
        .select_related("policy_version")
        .order_by("-policy_version__version", "-accepted_at")
        .first()
    )


def record_onboarding_acceptance(person, accepted, shown_version, *, request=None):
    if accepted:
        accept_shown_version(person, shown_version, request=request)
