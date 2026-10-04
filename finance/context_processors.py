from django.urls import reverse

from .google_auth import google_signin_enabled
from .ai_services import member_has_ai


def google_signin(_request):
    return {"google_signin_enabled": google_signin_enabled()}


def ai_features(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"show_ai_features": False, "chat_drawer": None}
    person = getattr(user, "person", None)
    if person is None:
        return {"show_ai_features": False, "chat_drawer": None}
    ready = member_has_ai(person)
    drawer = None
    if ready:
        from .chat_views import _chat_context
        from .chat_services import conversations_for

        conversation = conversations_for(person).first()
        drawer = _chat_context(person, conversation, request)
    return {"show_ai_features": ready, "chat_drawer": drawer}


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
    "revoke-session": "security",
    "revoke-other-sessions": "security",
    "simplefin-connections": "connections",
    "simplefin-sync": "connections",
    "simplefin-disconnect": "connections",
    "invite": "household",
    "leave-household": "household",
    "category-list": "categories",
    "category-rule-list": "categories",
    "category-rule-detail": "categories",
    "category-rule-application-reverse": "categories",
    "tag-list": "tags",
    "csv-mapping-list": "csv-mappings",
    "csv-mapping-edit": "csv-mappings",
    "settings-alerts": "alerts",
    "settings-data": "data",
    "account-export": "data",
    "delete-my-data": "data",
    "settings-ai": "ai",
    "ai-connect": "ai",
    "ai-disconnect": "ai",
    "ai-defaults": "ai",
    "ai-offer-local": "ai",
    "ai-shared-local": "ai",
}

SETTINGS_TABS = (
    ("security", "Sign-in & security", "account-settings"),
    ("connections", "Connections", "simplefin-connections"),
    ("household", "Household", "invite"),
    ("categories", "Categories", "category-list"),
    ("tags", "Tags", "tag-list"),
    ("csv-mappings", "CSV mappings", "csv-mapping-list"),
    ("alerts", "Alerts", "settings-alerts"),
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
        "transaction-note-tags",
    }:
        return "transaction-list"
    if name.startswith("csv-import"):
        return "csv-import"
    if name in {"planned-item-edit", "planned-item-disable", "planned-item-enable"}:
        return "planned-items"
    if name == "bills-calendar":
        return "bills-calendar"
    if name == "debt-payoff":
        return "debt-payoff"
    if name in {"monthly-review", "monthly-review-regenerate"}:
        return "monthly-review"
    if name in {"sheet-comparison", "sheet-comparison-delete"}:
        return "sheet-comparison"
    if name in {"year-end", "year-end-csv"}:
        return "year-end"
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
    if name.startswith("alert"):
        return "alert-list"
    if name in {"account-balances", "account-snapshot-edit", "account-snapshot-delete", "account-debt-terms"}:
        return "account-list"
    if name.startswith("chat"):
        return "chat"
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
    items = (
        ("home", "Cash flow", reverse("home")),
        ("net-worth", "Net worth", reverse("net-worth")),
        ("spending-by-category", "Spending", reverse("spending-by-category")),
        ("transaction-list", "Transactions", reverse("transaction-list")),
        ("chat", "Chat", reverse("chat")),
        ("transfer-review", "Transfers", reverse("transfer-review")),
        ("recurring-review", "Recurring", reverse("recurring-review")),
        ("account-list", "Accounts", reverse("account-list")),
        ("csv-import", "Import", reverse("csv-import")),
        ("planned-items", "Planned items", reverse("planned-items")),
        ("bills-calendar", "Bills", reverse("bills-calendar")),
        ("debt-payoff", "Debt payoff", reverse("debt-payoff")),
        ("monthly-review", "Monthly review", reverse("monthly-review")),
        ("sheet-comparison", "Sheet comparison", reverse("sheet-comparison")),
        ("year-end", "Year-end", reverse("year-end")),
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
