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
    "settings-audit": "audit",
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
    ("audit", "Audit trail", "settings-audit"),
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


# The one definition of the main navigation (issue #327): (URL name, label, icon).
# The lg sidebar shows every group; the phone dock shows the first group plus
# More, and the More page shows the rest, so the three cannot drift apart. Icons
# are stroke paths on a 24px box.
NAV_GROUPS = (
    (
        "main",
        "",
        (
            ("home", "Home", "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"),
            ("transaction-list", "Activity", "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"),
            ("budgets", "Budgets", "M21 12a9 9 0 1 1-9-9v9zM15 3.5A9 9 0 0 1 20.5 9H15z"),
            ("chat", "Chat", "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"),
        ),
    ),
    (
        "money",
        "Money",
        (
            ("net-worth", "Net worth", "m3 17 6-6 4 4 8-8M14 7h7v7"),
            ("spending-by-category", "Spending", "M4 20V11M10 20V5M16 20v-6M2 20h20"),
            ("account-list", "Accounts", "M3 10h18M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18M12 3l9 5H3z"),
            ("alert-list", "Alerts", "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"),
            (
                "recurring-review",
                "Recurring",
                "m17 2 4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3",
            ),
            ("transfer-review", "Transfers", "M4 7h16l-4-4M20 17H4l4 4"),
            (
                "bills-calendar",
                "Bills",
                "M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM16 3v4M8 3v4M3 10h18",
            ),
            (
                "savings-goals",
                "Goals",
                "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 11.5v1",
            ),
            ("csv-import", "Import", "M12 15V3M7 8l5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"),
        ),
    ),
    (
        "planning",
        "Planning",
        (
            (
                "planned-items",
                "Planned items",
                "M9 2h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2",
            ),
            ("debt-payoff", "Debt payoff", "m3 7 6 6 4-4 8 8M21 10v7h-7"),
            (
                "monthly-review",
                "Monthly review",
                "M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6M8 13h8M8 17h5",
            ),
            (
                "sheet-comparison",
                "Sheet comparison",
                "M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM12 4v16M3 10h18",
            ),
            ("year-end", "Year-end", "M4 21V4h12l-2 4 2 4H4"),
        ),
    ),
)


def navigation(request):
    if not getattr(request.user, "is_authenticated", False):
        return {"dock_tabs": [], "nav_items": [], "nav_groups": [], "nav_current": "", "settings_tabs": [], "settings_current": ""}
    current = _nav_current(request)
    settings_current = _settings_current(request)
    from .alert_services import unread_alert_count

    unread = unread_alert_count(request.user)
    nav_groups = []
    nav_items = []
    for group_key, group_label, entries in NAV_GROUPS:
        items = []
        for key, label, icon in entries:
            item = {"key": key, "label": label, "url": reverse(key), "icon": icon, "group": group_key, "active": key == current}
            if key == "alert-list":
                item["unread"] = unread
            items.append(item)
        nav_groups.append({"key": group_key, "label": group_label, "items": items})
        nav_items.extend(items)
    main_items = nav_groups[0]["items"]
    dock_tabs = [dict(item) for item in main_items]
    # Every other page belongs to More, so one tab is always current.
    dock_tabs.append(
        {
            "key": "more",
            "label": "More",
            "url": reverse("more"),
            "active": all(not item["active"] for item in main_items),
        }
    )
    return {
        "dock_tabs": dock_tabs,
        "nav_items": nav_items,
        "nav_groups": nav_groups,
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
