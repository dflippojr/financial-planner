"""Bounded, spreadsheet-safe export of the transaction list's parent rows."""

import csv
import json
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Prefetch
from django.http import HttpResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .forms import TransactionFilterForm
from .models import Category, Tag, TransactionSplit
from .reauth import recent_auth_is_fresh, reauth_redirect
from .security_services import EVENT_TYPES, record_security_event
from .transaction_filters import (
    apply_transaction_filters,
    query_params_from_cleaned,
    visible_transaction_queryset,
)


CHUNK_SIZE = 200
CSV_COLUMNS = (
    "transaction_id", "date", "account_name", "account_scope", "description",
    "amount_minor", "amount_decimal", "currency", "category_name", "category_source",
    "excluded_from_income_and_spending", "note", "tags", "splits", "import_source",
    "import_time",
)


class _Echo:
    def write(self, value):
        return value


def spreadsheet_text(value):
    """Escape text cells only; signed money remains numeric."""
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _category_name(obj):
    return obj.export_category.name if obj.export_category else ""


def iter_transaction_csv(transactions, principal):
    categories = Category.objects.visible_to(principal)
    transactions = transactions.select_related(None).select_related(
        "account", "import_batch",
    ).defer("original_fields", "fingerprint").prefetch_related(None).prefetch_related(
        Prefetch("category", queryset=categories, to_attr="export_category"),
        Prefetch("tags", queryset=Tag.objects.visible_to(principal).order_by("name", "pk"), to_attr="export_tags"),
        Prefetch("splits", queryset=TransactionSplit.objects.prefetch_related(
            Prefetch("category", queryset=categories, to_attr="export_category"),
        )),
    )
    writer = csv.writer(_Echo())
    yield writer.writerow(CSV_COLUMNS).encode("utf-8")
    for row in transactions.iterator(chunk_size=CHUNK_SIZE):
        tags = json.dumps([tag.name for tag in row.export_tags], ensure_ascii=False)
        splits = json.dumps([
            {"category_name": _category_name(part), "amount_minor": part.amount_minor, "currency": row.currency}
            for part in row.splits.all()
        ], ensure_ascii=False)
        yield writer.writerow((
            row.pk, row.transaction_date.isoformat(), spreadsheet_text(row.account.name),
            row.account.scope, spreadsheet_text(row.description), row.amount_minor,
            format(Decimal(row.amount_minor) / Decimal(100), ".2f"), row.currency,
            spreadsheet_text(_category_name(row)), row.category_source,
            str(row._excluded).lower(), spreadsheet_text(row.note), tags, splits,
            row.import_batch.source, row.import_batch.imported_at.isoformat(),
        )).encode("utf-8")


@require_POST
@never_cache
def transaction_export(request):
    form = TransactionFilterForm(request.POST, principal=request.user)
    if not form.is_valid():
        return HttpResponse("Invalid transaction filters.", status=400, content_type="text/plain")
    if not recent_auth_is_fresh(request):
        query = urlencode(query_params_from_cleaned(form.cleaned_data))
        return_url = reverse("transaction-list")
        if query:
            return_url += "?" + query
        return reauth_redirect(request, "export-data", return_url)
    transactions = apply_transaction_filters(
        visible_transaction_queryset(request.user), form.cleaned_data, request.user,
    )
    response = StreamingHttpResponse(
        iter_transaction_csv(transactions, request.user), content_type="text/csv; charset=utf-8",
    )
    filename = f"financial-planner-transactions-{timezone.localdate():%Y%m%d}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    record_security_event(request.user, EVENT_TYPES.MEMBER_DATA_EXPORT, request=request)
    return response
