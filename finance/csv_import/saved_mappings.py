"""Household saved CSV column mappings for providers without a built-in profile."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from finance.category_services import current_household
from finance.csv_import.parser import DATE_FORMATS, NUMBER_FORMATS, Mapping
from finance.lifecycle_services import _DENIED, lock_actor_household
from finance.models import Account, ImportBatch, Person, SavedCsvMapping


SAVED_PROFILE_PREFIX = "saved:"
HEADERS_DO_NOT_MATCH = (
    "This file's headers don't match the saved mapping. Map the columns by hand."
)
LOCKED_PARSING_MESSAGE = (
    "This mapping is locked after an import. Make a new mapping to change how files are parsed."
)
USED_MAPPING_DELETE_MESSAGE = "This mapping has imported a batch. Archive it instead of deleting it."
NO_HOUSEHOLD_MESSAGE = "Join a household to save CSV mappings."
NAME_TAKEN_MESSAGE = "The household already has an active mapping with that name."

PARSING_FIELD_NAMES = (
    "date_column",
    "date_format",
    "number_format",
    "amount_mode",
    "amount_column",
    "debit_column",
    "credit_column",
    "currency_column",
    "invert_sign",
    "description_mode",
    "description_column",
    "payee_column",
    "memo_column",
    "source_id_column",
    "excluded_original_columns",
)


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            pass
    raise PermissionDenied(_DENIED)


def saved_profile_key(mapping):
    return f"{SAVED_PROFILE_PREFIX}{mapping.pk}"


def parse_saved_profile(value):
    if not isinstance(value, str) or not value.startswith(SAVED_PROFILE_PREFIX):
        return None
    suffix = value[len(SAVED_PROFILE_PREFIX) :]
    if not suffix.isdigit():
        return None
    return int(suffix)


def mapping_from_saved(saved):
    return Mapping(
        date_column=saved.date_column,
        description_column=saved.description_column,
        date_format=saved.date_format,
        number_format=saved.number_format,
        amount_mode=saved.amount_mode,
        amount_column=saved.amount_column,
        debit_column=saved.debit_column,
        credit_column=saved.credit_column,
        currency_column=saved.currency_column,
        invert_sign=saved.invert_sign,
        description_mode=saved.description_mode,
        payee_column=saved.payee_column,
        memo_column=saved.memo_column,
        source_id_column=saved.source_id_column,
        excluded_original_columns=tuple(saved.excluded_original_columns),
    )


def headers_match(saved, headers):
    return tuple(saved.headers) == tuple(headers)


def active_saved_mappings(principal):
    return (
        SavedCsvMapping.objects.visible_to(principal)
        .filter(status=SavedCsvMapping.Status.ACTIVE, archived_at__isnull=True)
        .order_by("name", "pk")
    )


def visible_saved_mapping(principal, mapping_id):
    return (
        SavedCsvMapping.objects.visible_to(principal)
        .filter(pk=mapping_id)
        .first()
    )


def _require_parsing_keys(mapping: Mapping):
    if mapping.date_format not in DATE_FORMATS:
        raise ValidationError("Choose a supported date format.")
    if mapping.number_format not in NUMBER_FORMATS:
        raise ValidationError("Choose a supported number format.")
    if mapping.amount_mode not in ("signed", "separate"):
        raise ValidationError("Choose how amounts are stored in the file.")
    if mapping.description_mode not in ("column", "payee_memo"):
        raise ValidationError("Choose how the description is built.")


def _fields_from_mapping(mapping: Mapping):
    _require_parsing_keys(mapping)
    return {
        "date_column": mapping.date_column,
        "description_column": mapping.description_column,
        "date_format": mapping.date_format,
        "number_format": mapping.number_format,
        "amount_mode": mapping.amount_mode,
        "amount_column": mapping.amount_column or "",
        "debit_column": mapping.debit_column or "",
        "credit_column": mapping.credit_column or "",
        "currency_column": mapping.currency_column or "",
        "invert_sign": bool(mapping.invert_sign),
        "description_mode": mapping.description_mode,
        "payee_column": mapping.payee_column or "",
        "memo_column": mapping.memo_column or "",
        "source_id_column": mapping.source_id_column or "",
        "excluded_original_columns": list(mapping.excluded_original_columns),
    }


def _name_taken(household, name, *, exclude_pk=None):
    query = SavedCsvMapping.objects.filter(
        household=household,
        name=name,
        status=SavedCsvMapping.Status.ACTIVE,
        archived_at__isnull=True,
    )
    if exclude_pk is not None:
        query = query.exclude(pk=exclude_pk)
    return query.exists()


def _clear_account_defaults(mapping):
    Account.objects.filter(default_saved_csv_mapping=mapping).update(default_saved_csv_mapping=None)


@transaction.atomic
def save_csv_mapping(principal, *, name, headers, mapping, account=None, set_as_account_default=False):
    person = _person_for(principal)
    lock_actor_household(person)
    household = current_household(person)
    if household is None:
        raise ValidationError(NO_HOUSEHOLD_MESSAGE)
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValidationError("Name this mapping to save it.")
    if _name_taken(household, trimmed):
        raise ValidationError(NAME_TAKEN_MESSAGE)
    header_list = list(headers)
    saved = SavedCsvMapping.objects.create(
        household=household,
        name=trimmed,
        headers=header_list,
        created_by=person,
        **_fields_from_mapping(mapping),
    )
    if set_as_account_default and account is not None:
        set_account_default_mapping(person, account.pk, saved.pk)
    return saved


def _parsing_changed(saved, mapping: Mapping):
    incoming = _fields_from_mapping(mapping)
    for field in PARSING_FIELD_NAMES:
        if incoming[field] != getattr(saved, field):
            return True
    return False


@transaction.atomic
def update_csv_mapping(principal, mapping_id, *, name, mapping=None, default_account_ids=None):
    person = _person_for(principal)
    lock_actor_household(person)
    saved = (
        SavedCsvMapping.objects.select_for_update()
        .filter(pk=mapping_id, household=current_household(person))
        .first()
    )
    if saved is None:
        raise PermissionDenied(_DENIED)
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValidationError("Name this mapping.")
    if _name_taken(saved.household, trimmed, exclude_pk=saved.pk):
        raise ValidationError(NAME_TAKEN_MESSAGE)
    saved.name = trimmed
    if mapping is not None:
        if saved.locked_at is not None and _parsing_changed(saved, mapping):
            raise ValidationError(LOCKED_PARSING_MESSAGE)
        if saved.locked_at is None:
            for field, value in _fields_from_mapping(mapping).items():
                setattr(saved, field, value)
    saved.save()
    if default_account_ids is not None:
        replace_mapping_account_defaults(person, saved, default_account_ids)
    return saved


@transaction.atomic
def set_account_default_mapping(principal, account_id, mapping_id):
    person = _person_for(principal)
    lock_actor_household(person)
    account = Account.objects.visible_to(person).filter(pk=account_id, status=Account.Status.ACTIVE).first()
    if account is None:
        raise PermissionDenied(_DENIED)
    if mapping_id is None:
        account.default_saved_csv_mapping = None
        account.save(update_fields=("default_saved_csv_mapping", "updated_at"))
        return account
    saved = active_saved_mappings(person).filter(pk=mapping_id).first()
    if saved is None:
        raise PermissionDenied(_DENIED)
    account.default_saved_csv_mapping = saved
    account.save(update_fields=("default_saved_csv_mapping", "updated_at"))
    return account


@transaction.atomic
def replace_mapping_account_defaults(principal, saved, account_ids):
    person = _person_for(principal)
    wanted = set(account_ids)
    visible = {
        account.pk: account
        for account in Account.objects.visible_to(person).filter(status=Account.Status.ACTIVE)
    }
    if wanted - set(visible):
        raise PermissionDenied(_DENIED)
    for account in visible.values():
        if account.pk in wanted:
            if account.default_saved_csv_mapping_id != saved.pk:
                account.default_saved_csv_mapping = saved
                account.save(update_fields=("default_saved_csv_mapping", "updated_at"))
        elif account.default_saved_csv_mapping_id == saved.pk:
            account.default_saved_csv_mapping = None
            account.save(update_fields=("default_saved_csv_mapping", "updated_at"))


def mapping_has_batches(saved):
    return ImportBatch.objects.filter(saved_csv_mapping=saved).exists()


@transaction.atomic
def delete_or_archive_csv_mapping(principal, mapping_id):
    person = _person_for(principal)
    lock_actor_household(person)
    saved = (
        SavedCsvMapping.objects.select_for_update()
        .filter(pk=mapping_id, household=current_household(person))
        .first()
    )
    if saved is None:
        raise PermissionDenied(_DENIED)
    _clear_account_defaults(saved)
    if mapping_has_batches(saved):
        if saved.status != SavedCsvMapping.Status.ARCHIVED:
            saved.status = SavedCsvMapping.Status.ARCHIVED
            saved.archived_at = timezone.now()
            saved.save(update_fields=("status", "archived_at", "updated_at"))
        return saved
    saved.delete()
    return None


@transaction.atomic
def lock_saved_mapping(saved):
    if saved is None or saved.locked_at is not None:
        return saved
    saved.locked_at = timezone.now()
    saved.save(update_fields=("locked_at", "updated_at"))
    return saved
