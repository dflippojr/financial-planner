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


SETTINGS_TAB_BY_NAME = {
    "account-settings": "security",
    "simplefin-connections": "connections",
    "simplefin-sync": "connections",
    "simplefin-disconnect": "connections",
    "invite": "household",
    "leave-household": "household",
    "category-list": "categories",
    "category-rule-list": "categories",
    "category-rule-detail": "categories",
    "category-rule-application-reverse": "categories",
    "settings-data": "data",
    "account-export": "data",
    "settings-ai": "ai",
    "ai-connect": "ai",
    "ai-disconnect": "ai",
    "ai-defaults": "ai",
}

SETTINGS_TABS = (
    ("security", "Sign-in & security", "account-settings"),
    ("connections", "Connections", "simplefin-connections"),
    ("household", "Household", "invite"),
    ("categories", "Categories", "category-list"),
    ("data", "Data", "settings-data"),
    ("ai", "AI", "settings-ai"),
)


def _nav_current(request):
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", "") or ""
    if name in SETTINGS_TAB_BY_NAME or name.startswith("category-rule") or name.startswith("simplefin"):
        return "settings"
    if name in {
        "transaction-edit",
        "transaction-categorize",
        "transaction-link-refund",
        "transaction-split",
        "transaction-unsplit",
        "transaction-split-part-category",
    }:
        return "transaction-list"
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
    if name in {"account-balances", "account-snapshot-edit", "account-snapshot-delete"}:
        return "account-list"
    return name


def _settings_current(request):
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", "") or ""
    if name in SETTINGS_TAB_BY_NAME:
        return SETTINGS_TAB_BY_NAME[name]
    if name.startswith("category-rule"):
        return "categories"
    if name.startswith("simplefin"):
        return "connections"
    return ""


def navigation(request):
    if not getattr(request.user, "is_authenticated", False):
        return {"nav_items": [], "nav_current": "", "settings_tabs": [], "settings_current": ""}
    current = _nav_current(request)
    settings_current = _settings_current(request)
    accounts_url = reverse("account-list")
    items = (
        ("home", "Cash flow", reverse("home")),
        ("net-worth", "Net worth", reverse("net-worth")),
        ("spending-by-category", "Spending", reverse("spending-by-category")),
        ("transaction-list", "Transactions", reverse("transaction-list")),
        ("transfer-review", "Transfers", reverse("transfer-review")),
        ("recurring-review", "Recurring", reverse("recurring-review")),
        ("account-list", "Accounts", accounts_url),
        ("csv-import", "Import", accounts_url),
        ("planned-items", "Planned items", reverse("planned-items")),
        ("budgets", "Budgets", reverse("budgets")),
        ("savings-goals", "Goals", reverse("savings-goals")),
    )
    return {
        "nav_items": [
            {"key": key, "label": label, "url": url, "active": key == current} for key, label, url in items
        ],
        "nav_current": current,
        "settings_current": settings_current,
        "settings_tabs": [
            {
                "key": key,
                "label": label,
                "url": reverse(url_name),
                "active": key == settings_current,
            }
            for key, label, url_name in SETTINGS_TABS
        ],
    }
