from django.urls import reverse

from .google_auth import google_signin_enabled
from .ai_services import member_has_ai


def google_signin(_request):
    return {"google_signin_enabled": google_signin_enabled()}


def ai_features(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"show_ai_features": False}
    person = getattr(user, "person", None)
    if person is None:
        return {"show_ai_features": False}
    return {"show_ai_features": member_has_ai(person)}


def google_signin(_request):
    return {"google_signin_enabled": google_signin_enabled()}


def privacy_policy_prompt(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"privacy_policy_prompt": None}
    person = getattr(user, "person", None)
    if person is None:
        return {"privacy_policy_prompt": None}
    from .policy_services import current_policy, should_prompt_privacy_policy

    if not should_prompt_privacy_policy(person):
        return {"privacy_policy_prompt": None}
    return {"privacy_policy_prompt": current_policy()}


def _nav_current(request):
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", "") or ""
    if name in {
        "transaction-edit",
        "transaction-categorize",
        "transaction-link-refund",
        "transaction-split",
        "transaction-unsplit",
        "transaction-split-part-category",
    }:
        return "transaction-list"
    if name.startswith("category-rule"):
        return "category-list"
    if name.startswith("csv-import"):
        return "csv-import"
    if name in {"planned-item-edit", "planned-item-disable", "planned-item-enable"}:
        return "planned-items"
    if name.startswith("budget"):
        return "budgets"
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
    if name.startswith("alert"):
        return "alert-list"
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
        ("budgets", "Budgets", reverse("budgets")),
        ("alert-list", "Alerts", reverse("alert-list")),
        ("savings-goals", "Goals", reverse("savings-goals")),
    )
    from .alert_services import unread_alert_count

    unread = unread_alert_count(request.user)
    nav_items = []
    for key, label, url in items:
        item = {"key": key, "label": label, "url": url, "active": key == current}
        if key == "alert-list":
            item["unread"] = unread
        nav_items.append(item)
    return {
        "nav_items": nav_items,
        "nav_current": current,
    }
