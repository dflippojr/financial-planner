from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Abs
from django.urls import reverse

from .access import DENIED as _DENIED
from .category_services import exclusion_exists_for
from .models import (
    Account,
    Category,
    CategorySuggestion,
    SavedTransactionFilter,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
    _person_for,
)
from .tag_services import apply_tag_filter

PAGE_SIZE = 100
PAYEE_MEMO_KEYS = ("payee", "Payee", "memo", "Memo")
AMOUNT_MODE_SIGNED = "signed"
AMOUNT_MODE_ABSOLUTE = "absolute"
SET_BY_HAND = "hand"
SET_BY_RULE = "rule"
SET_BY_SUGGESTION = "suggestion"
FLAG_YES = "1"
FILTER_PARAM_KEYS = (
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


def apply_transaction_filters(transactions, filters, principal):
    if filters.get("date_from"):
        transactions = transactions.filter(transaction_date__gte=filters["date_from"])
    if filters.get("date_to"):
        transactions = transactions.filter(transaction_date__lte=filters["date_to"])
    if filters.get("account"):
        transactions = transactions.filter(account=filters["account"])
    if filters.get("scope"):
        transactions = transactions.filter(account__scope=filters["scope"])
    if filters.get("q"):
        transactions = transactions.filter(_search_q(filters["q"]))
    if filters.get("tag"):
        transactions = apply_tag_filter(transactions, filters["tag"])
    transactions = _apply_amount_filters(transactions, filters)
    if filters.get("has_note") == FLAG_YES:
        transactions = transactions.exclude(note="")
    if filters.get("is_split") == FLAG_YES:
        transactions = transactions.filter(category_source=Transaction.CategorySource.SPLIT)
    transactions = _apply_set_by(transactions, filters.get("set_by") or "")
    category = filters.get("category")
    if category == "uncategorized":
        return transactions.filter(
            Q(category__isnull=True)
            | Q(category__code=Category.Code.UNCATEGORIZED)
            | ~Q(category__in=Category.objects.visible_to(principal))
        ).exclude(_excluded=True).exclude(category_source=Transaction.CategorySource.SPLIT)
    if category == "transfer":
        return transactions.filter(_excluded=True)
    if category:
        return transactions.filter(
            Q(category_id=category) | Q(splits__category_id=category)
        ).exclude(_excluded=True).distinct()
    return transactions


def paginate_transactions(transactions, page_number):
    paginator = Paginator(transactions, PAGE_SIZE)
    return paginator.get_page(page_number)


def query_params_from_cleaned(cleaned):
    params = {}
    date_from = cleaned.get("date_from")
    date_to = cleaned.get("date_to")
    if date_from:
        params["date_from"] = date_from.isoformat()
    if date_to:
        params["date_to"] = date_to.isoformat()
    account = cleaned.get("account")
    if account is not None:
        params["account"] = str(account.pk)
    category = cleaned.get("category")
    if category:
        params["category"] = str(category)
    query = (cleaned.get("q") or "").strip()
    if query:
        params["q"] = query
    tag = cleaned.get("tag")
    if tag is not None:
        params["tag"] = str(tag.pk)
    scope = cleaned.get("scope") or ""
    if scope:
        params["scope"] = scope
    amount_min = cleaned.get("amount_min")
    amount_max = cleaned.get("amount_max")
    if amount_min is not None:
        params["amount_min"] = str(amount_min)
    if amount_max is not None:
        params["amount_max"] = str(amount_max)
    amount_mode = cleaned.get("amount_mode") or ""
    if amount_mode and (amount_min is not None or amount_max is not None):
        params["amount_mode"] = amount_mode
    if cleaned.get("has_note") == FLAG_YES:
        params["has_note"] = FLAG_YES
    if cleaned.get("is_split") == FLAG_YES:
        params["is_split"] = FLAG_YES
    set_by = cleaned.get("set_by") or ""
    if set_by:
        params["set_by"] = set_by
    return params


def sanitize_saved_query(principal, query):
    if not isinstance(query, dict):
        return {}
    params = {key: value for key, value in query.items() if key in FILTER_PARAM_KEYS and isinstance(value, str)}
    account = params.get("account")
    if account and not Account.objects.visible_to(principal).filter(pk=account).exists():
        params.pop("account", None)
    tag = params.get("tag")
    if tag and not Tag.objects.visible_to(principal).filter(pk=tag).exists():
        params.pop("tag", None)
    return params


def save_transaction_filter(principal, *, name, query):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValidationError("Enter a name for this filter set.")
    if len(cleaned_name) > 80:
        raise ValidationError("Filter names must be 80 characters or fewer.")
    params = sanitize_saved_query(person, query)
    try:
        return SavedTransactionFilter.objects.create(member=person, name=cleaned_name, query=params)
    except IntegrityError as exc:
        raise ValidationError("A saved filter with that name already exists.") from exc


def delete_saved_transaction_filter(principal, filter_id):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    deleted, _counts = SavedTransactionFilter.objects.visible_to(person).filter(pk=filter_id).delete()
    if not deleted:
        raise PermissionDenied(_DENIED)


def apply_saved_filter_url(principal, filter_id):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    saved = SavedTransactionFilter.objects.visible_to(person).filter(pk=filter_id).first()
    if saved is None:
        raise PermissionDenied(_DENIED)
    params = sanitize_saved_query(person, saved.query)
    encoded = urlencode(params)
    path = reverse("transaction-list")
    return f"{path}?{encoded}" if encoded else path


def visible_transaction_queryset(principal):
    return (
        Transaction.objects.visible_to(principal)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "import_batch", "category")
        .prefetch_related("splits__category", "tags")
        .annotate(_excluded=exclusion_exists_for(principal))
        .order_by("-transaction_date", "-pk")
    )


def _search_q(needle):
    match = Q(description__icontains=needle) | Q(note__icontains=needle)
    for key in PAYEE_MEMO_KEYS:
        match |= Q(**{f"original_fields__{key}__icontains": needle})
    return match


def _minor_bound(amount):
    if amount is None:
        return None
    return int(amount * 100)


def _apply_amount_filters(transactions, filters):
    raw_min = _minor_bound(filters.get("amount_min"))
    raw_max = _minor_bound(filters.get("amount_max"))
    if raw_min is None and raw_max is None:
        return transactions
    mode = filters.get("amount_mode") or AMOUNT_MODE_SIGNED
    if mode == AMOUNT_MODE_ABSOLUTE:
        annotated = transactions.annotate(_amount_abs=Abs("amount_minor"))
        abs_min = None if raw_min is None else abs(raw_min)
        abs_max = None if raw_max is None else abs(raw_max)
        if abs_min is not None:
            annotated = annotated.filter(_amount_abs__gte=abs_min)
        if abs_max is not None:
            annotated = annotated.filter(_amount_abs__lte=abs_max)
        return annotated
    if raw_min is not None:
        transactions = transactions.filter(amount_minor__gte=raw_min)
    if raw_max is not None:
        transactions = transactions.filter(amount_minor__lte=raw_max)
    return transactions


def _accepted_suggestion_exists():
    """The current category came from an accepted suggestion with no category change since.

    Accepting writes its own history row before the suggestion is resolved, so any
    category history recorded after `resolved_at` is a later change by someone.
    """
    changed_since = TransactionCorrectionHistory.objects.filter(
        transaction_id=OuterRef(OuterRef("pk")),
        field_name=TransactionCorrectionHistory.Field.CATEGORY,
        recorded_at__gt=OuterRef("resolved_at"),
    )
    return Exists(
        CategorySuggestion.objects.filter(
            transaction_id=OuterRef("pk"),
            status=CategorySuggestion.Status.ACCEPTED,
            category_id=OuterRef("category_id"),
        ).exclude(Exists(changed_since))
    )


def _apply_set_by(transactions, set_by):
    if not set_by:
        return transactions
    if set_by == SET_BY_RULE:
        return transactions.filter(category_source=Transaction.CategorySource.RULE)
    if set_by == SET_BY_SUGGESTION:
        return transactions.filter(category_source=Transaction.CategorySource.MANUAL).filter(
            _accepted_suggestion_exists()
        )
    if set_by == SET_BY_HAND:
        return transactions.filter(category_source=Transaction.CategorySource.MANUAL).exclude(
            _accepted_suggestion_exists()
        )
    return transactions


def filter_hidden_pairs(cleaned):
    return list(query_params_from_cleaned(cleaned).items())
