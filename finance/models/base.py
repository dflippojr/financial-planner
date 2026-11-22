
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models



sha256_validator = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="Enter a lowercase hexadecimal SHA-256 digest.",
)


receipt_stored_name_validator = RegexValidator(
    regex=r"^[0-9a-f]{32}$",
    message="Enter a 32-character hexadecimal stored name.",
)


def validate_json_object(value):
    if not isinstance(value, dict):
        raise ValidationError("Original imported fields must be a JSON object.")


def validate_bulk_edit_snapshot(value):
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        raise ValidationError("Bulk edit snapshot must be an object with a row list.")


SAVED_FILTER_QUERY_KEYS = frozenset(
    (
        "date_from",
        "date_to",
        "account",
        "category",
        "q",
        "tag",
        "scope",
        "amount_min",
        "amount_max",
        "amount_mode",
        "has_note",
        "is_split",
        "set_by",
    )
)


def validate_saved_filter_query(value):
    if not isinstance(value, dict):
        raise ValidationError("Saved filter query must be an object.")
    for key, item in value.items():
        if key not in SAVED_FILTER_QUERY_KEYS or not isinstance(item, str):
            raise ValidationError("Saved filter query is invalid.")


def validate_reason_list(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("Reasons must be a list of strings.")


FORMER_MEMBER_LABEL = "former member"


def actor_display_name(person):
    if person is None:
        return FORMER_MEMBER_LABEL
    return person.display_name


def validate_header_name_list(value):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValidationError("Column names must be a list of non-empty strings.")


def validate_stored_csv_headers(value):
    validate_header_name_list(value)
    if not value:
        raise ValidationError("A saved mapping must store the file's header list.")


class ArchivableModel(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    status = models.CharField(max_length=8, choices=Status, default=Status.ACTIVE)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

