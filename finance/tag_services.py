from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower

from .access import DENIED as _DENIED
from .audit_services import record
from .category_services import current_household
from .lifecycle_services import lock_actor_household
from .models import Account, AuditEvent, Tag, Transaction, _person_for

TAG_NAME_ERROR = "Enter a tag name."
TAG_EXISTS = "A tag with that name already exists."
NOTE_TOO_LONG = "Notes must be 2,000 characters or fewer."


def apply_tag_filter(queryset, tag):
    if tag is None:
        return queryset
    return queryset.filter(tags=tag).distinct()


def selected_tag(form):
    if form.is_bound and form.is_valid():
        return form.cleaned_data.get("tag")
    return None


def _cleaned_tag_name(name):
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError(TAG_NAME_ERROR)
    if len(cleaned) > 80:
        raise ValidationError("Tag names must be 80 characters or fewer.")
    return cleaned


def _name_taken(household, name, *, exclude_pk=None):
    tags = household.tags.annotate(_lower=Lower("name")).filter(_lower=name.lower())
    if exclude_pk is not None:
        tags = tags.exclude(pk=exclude_pk)
    return tags.exists()


@transaction.atomic
def add_tag(principal, name):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    cleaned = _cleaned_tag_name(name)
    if _name_taken(household, cleaned):
        raise ValidationError(TAG_EXISTS)
    try:
        tag = Tag.objects.create(household=household, name=cleaned)
    except IntegrityError as exc:
        raise ValidationError(TAG_EXISTS) from exc
    record(person, AuditEvent.Action.RECORD_CREATED, AuditEvent.TargetType.TAG, tag.pk,
           audience={"household": household})
    return tag


@transaction.atomic
def rename_tag(principal, tag_id, name):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    tag = Tag.objects.visible_to(person).select_for_update().filter(pk=tag_id).first()
    if tag is None:
        raise PermissionDenied(_DENIED)
    cleaned = _cleaned_tag_name(name)
    if _name_taken(household, cleaned, exclude_pk=tag.pk):
        raise ValidationError(TAG_EXISTS)
    renamed = tag.name != cleaned
    tag.name = cleaned
    try:
        tag.save(update_fields=("name", "updated_at"))
    except IntegrityError as exc:
        raise ValidationError(TAG_EXISTS) from exc
    if renamed:
        record(person, AuditEvent.Action.RECORD_EDITED, AuditEvent.TargetType.TAG, tag.pk,
               audience={"household": household}, fields=("name",))
    return tag


@transaction.atomic
def archive_tag(principal, tag_id):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    tag = Tag.objects.visible_to(person).select_for_update().filter(pk=tag_id).first()
    if tag is None:
        raise PermissionDenied(_DENIED)
    if not tag.is_archived:
        tag.is_archived = True
        tag.save(update_fields=("is_archived", "updated_at"))
        record(person, AuditEvent.Action.RECORD_ARCHIVED, AuditEvent.TargetType.TAG, tag.pk,
               audience={"household": household}, fields=("status",))
    return tag


@transaction.atomic
def set_transaction_note_and_tags(principal, transaction_id, *, note, tag_ids, new_tag_name=""):
    person = _person_for(principal)
    visible = (
        Transaction.objects.visible_to(person)
        .filter(pk=transaction_id, status=Transaction.Status.ACTIVE)
        .first()
    )
    if visible is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    account = (
        Account.objects.visible_to(person).select_for_update().filter(pk=visible.account_id).first()
    )
    if account is None:
        raise PermissionDenied(_DENIED)
    txn = (
        Transaction.objects.select_for_update()
        .filter(pk=visible.pk, account=account, status=Transaction.Status.ACTIVE)
        .first()
    )
    if txn is None:
        raise PermissionDenied(_DENIED)
    cleaned_note = note or ""
    if len(cleaned_note) > 2000:
        raise ValidationError(NOTE_TOO_LONG)
    household = current_household(person)
    requested = []
    for item in tag_ids or []:
        requested.append(item.pk if hasattr(item, "pk") else int(item))
    selected = []
    if new_tag_name and str(new_tag_name).strip():
        if household is None:
            raise PermissionDenied(_DENIED)
        selected.append(add_tag(person, new_tag_name))
    if requested:
        if household is None:
            raise PermissionDenied(_DENIED)
        active = list(
            Tag.objects.visible_to(person).active().filter(pk__in=requested)
        )
        if {tag.pk for tag in active} != set(requested):
            raise PermissionDenied(_DENIED)
        selected.extend(active)
    kept_archived = list(txn.tags.filter(is_archived=True))
    previous_tag_ids = set(txn.tags.values_list("pk", flat=True))
    txn.note = cleaned_note
    txn.save(update_fields=("note", "updated_at"))
    by_id = {tag.pk: tag for tag in kept_archived + selected}
    txn.tags.set(list(by_id.values()))
    if previous_tag_ids != set(by_id):
        # Tag membership only; ordinary note edits deliberately have no event (#98).
        record(person, AuditEvent.Action.TAGS_CHANGED, AuditEvent.TargetType.TRANSACTION, txn.pk,
               audience={"account": account}, fields=("tags",))
    return txn
