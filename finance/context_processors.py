from django.urls import reverse

from .google_auth import google_signin_enabled


def google_signin(_request):
    return {"google_signin_enabled": google_signin_enabled()}


def _nav_current(request):
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", "") or ""
    if name in {"transaction-edit", "transaction-categorize", "transaction-link-refund"}:
        return "transaction-list"
    if name.startswith("category-rule"):
        return "category-list"
    if name.startswith("csv-import"):
        return "csv-import"
    if name in {"planned-item-edit", "planned-item-disable", "planned-item-enable"}:
        return "planned-items"
    if name in {
        "savings-goal-edit",
        "savings-goal-complete",
        "savings-goal-reopen",
        "savings-goal-archive",
        "savings-goal-unarchive",
    }:
        return "savings-goals"
    if name.startswith("simplefin"):
        return "simplefin-connections"
    if name in {"account-balances", "account-snapshot-edit", "account-snapshot-delete"}:
        return "account-list"
    return name


def navigation(request):
    if not getattr(request.user, "is_authenticated", False):
        return {"nav_items": [], "nav_current": ""}
    current = _nav_current(request)
    accounts_url = reverse("account-list")
    items = (
        ("home", "Cash flow", reverse("home")),
        ("net-worth", "Net worth", reverse("net-worth")),
        ("spending-by-category", "Spending", reverse("spending-by-category")),
        ("transaction-list", "Transactions", reverse("transaction-list")),
        ("transfer-review", "Transfers", reverse("transfer-review")),
        ("recurring-review", "Recurring", reverse("recurring-review")),
        ("category-list", "Categories", reverse("category-list")),
        ("account-list", "Accounts", accounts_url),
        ("simplefin-connections", "Connections", reverse("simplefin-connections")),
        ("csv-import", "Import", accounts_url),
        ("invite", "Invite", reverse("invite")),
        ("planned-items", "Planned items", reverse("planned-items")),
        ("savings-goals", "Goals", reverse("savings-goals")),
    )
    return {
        "nav_items": [
            {"key": key, "label": label, "url": url, "active": key == current} for key, label, url in items
        ],
        "nav_current": current,
    }
