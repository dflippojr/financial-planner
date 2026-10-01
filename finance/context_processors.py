from django.urls import reverse

from .google_auth import google_signin_enabled
from .models import Account


def google_signin(_request):
    return {"google_signin_enabled": google_signin_enabled()}


def _nav_current(request):
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", "") or ""
    if name in {"transaction-edit", "transaction-categorize", "transaction-link-refund"}:
        return "transaction-list"
    if name.startswith("csv-import"):
        return "csv-import"
    return name


def navigation(request):
    if not getattr(request.user, "is_authenticated", False):
        return {"nav_items": [], "nav_current": ""}
    current = _nav_current(request)
    import_account = (
        Account.objects.visible_to(request.user)
        .filter(status=Account.Status.ACTIVE, archived_at=None)
        .order_by("name", "pk")
        .first()
    )
    import_url = reverse("csv-import-preview", args=(import_account.pk,)) if import_account else reverse("home")
    items = (
        ("home", "Cash flow", reverse("home")),
        ("spending-by-category", "Spending", reverse("spending-by-category")),
        ("transaction-list", "Transactions", reverse("transaction-list")),
        ("transfer-review", "Transfers", reverse("transfer-review")),
        ("recurring-review", "Recurring", reverse("recurring-review")),
        ("category-list", "Categories", reverse("category-list")),
        ("csv-import", "Import", import_url),
        ("invite", "Invite", reverse("invite")),
    )
    return {
        "nav_items": [
            {"key": key, "label": label, "url": url, "active": key == current} for key, label, url in items
        ],
        "nav_current": current,
    }
