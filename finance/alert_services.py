from calendar import month_name
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, prefetch_related_objects
from django.urls import reverse
from django.utils import timezone

from finance.models import _person_for

from .access import DENIED as _DENIED
from .alert_email import notify_after_alert_run
from .audit_services import append_event
from .budget_services import progress_snapshot, progress_snapshots
from .cash_flow import format_minor
from .models import (
    Account,
    Alert,
    AlertSettings,
    AuditEvent,
    Budget,
    Person,
    SimpleFinConnection,
    Transaction,
)
from .months import month_start

_KIND_ENABLED_FIELD = {
    Alert.Kind.SYNC: "sync_enabled",
    Alert.Kind.RECURRING_PRICE: "recurring_price_enabled",
    Alert.Kind.RECURRING_MISSED: "recurring_missed_enabled",
    Alert.Kind.BUDGET: "budget_enabled",
    Alert.Kind.LARGE_TRANSACTION: "large_transaction_enabled",
    Alert.Kind.MONTHLY_REVIEW: "monthly_review_enabled",
    Alert.Kind.EXPECTED_BALANCE: "expected_balance_enabled",
    Alert.Kind.UNUSUAL_SPENDING: "unusual_spending_enabled",
}
READ_RETENTION_DAYS = 180


def settings_for(person):
    prefs, _created = AlertSettings.objects.get_or_create(person=person)
    return prefs


def kind_is_enabled(prefs, kind):
    if kind == Alert.Kind.BACKUP:
        return True
    field = _KIND_ENABLED_FIELD.get(kind)
    return bool(field and getattr(prefs, field))


def _internal_link(link):
    return link.startswith("/") and not link.startswith("//")


def raise_alert(recipients, kind, title, link, dedupe_key, account=None):
    """Create one inbox row per recipient unless that condition was already raised."""
    if not _internal_link(link):
        raise ValidationError("Alert links must be internal URLs.")
    created = []
    for recipient in recipients:
        prefs = settings_for(recipient)
        if not kind_is_enabled(prefs, kind):
            continue
        alert, was_created = Alert.objects.get_or_create(
            recipient=recipient,
            dedupe_key=dedupe_key,
            defaults={
                "kind": kind,
                "title": title[:200],
                "link": link[:500],
                "account": account,
            },
        )
        if was_created:
            created.append(alert)
    return created


def _budget_id_from_dedupe(dedupe_key):
    parts = dedupe_key.split(":")
    if len(parts) < 2 or parts[0] != "budget" or not parts[1].isdigit():
        return None
    return int(parts[1])


def alerts_for(principal):
    person = _person_for(principal)
    if person is None:
        return Alert.objects.none()
    visible_accounts = Account.objects.visible_to(person).values("pk")
    visible_budgets = set(Budget.objects.visible_to(person).values_list("pk", flat=True))
    kept = []
    rows = (
        Alert.objects.filter(recipient=person)
        .exclude(kind=Alert.Kind.LARGE_TRANSACTION, account_id__isnull=True)
        .filter(Q(account_id__isnull=True) | Q(account_id__in=visible_accounts))
        .order_by("-created_at", "-pk")
    )
    for alert in rows:
        if alert.kind == Alert.Kind.BUDGET:
            budget_id = _budget_id_from_dedupe(alert.dedupe_key)
            if budget_id not in visible_budgets:
                continue
        kept.append(alert.pk)
    return Alert.objects.filter(pk__in=kept).order_by("-created_at", "-pk")


def unread_alert_count(principal):
    return alerts_for(principal).filter(read_at__isnull=True).count()


def mark_alert_read(principal, alert_id):
    alert = alerts_for(principal).filter(pk=alert_id).first()
    if alert is None:
        raise PermissionDenied(_DENIED)
    if alert.read_at is None:
        alert.read_at = timezone.now()
        alert.save(update_fields=("read_at",))
    return alert


def mark_all_alerts_read(principal):
    now = timezone.now()
    return alerts_for(principal).filter(read_at__isnull=True).update(read_at=now)


_ALERT_TOGGLES = (
    "sync_enabled", "recurring_price_enabled", "recurring_missed_enabled", "budget_enabled",
    "large_transaction_enabled", "monthly_review_enabled", "monthly_review_ai_enabled",
    "expected_balance_enabled", "unusual_spending_enabled", "unusual_spending_ai_enabled",
)
_ALERT_THRESHOLDS = ("unusual_category_percent", "unusual_category_floor_minor", "large_transaction_minor")


@transaction.atomic
def save_alert_settings(
    principal,
    *,
    sync_enabled,
    recurring_price_enabled,
    recurring_missed_enabled,
    budget_enabled,
    large_transaction_enabled,
    monthly_review_enabled,
    monthly_review_ai_enabled,
    large_transaction_minor,
    expected_balance_enabled=False,
    unusual_spending_enabled=True,
    unusual_spending_ai_enabled=True,
    unusual_category_percent=50,
    unusual_category_floor_minor=5_000,
):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    prefs = settings_for(person)
    before = {name: getattr(prefs, name) for name in (*_ALERT_TOGGLES, *_ALERT_THRESHOLDS)}
    prefs.sync_enabled = bool(sync_enabled)
    prefs.recurring_price_enabled = bool(recurring_price_enabled)
    prefs.recurring_missed_enabled = bool(recurring_missed_enabled)
    prefs.budget_enabled = bool(budget_enabled)
    prefs.large_transaction_enabled = bool(large_transaction_enabled)
    prefs.monthly_review_enabled = bool(monthly_review_enabled)
    prefs.monthly_review_ai_enabled = bool(monthly_review_ai_enabled)
    prefs.expected_balance_enabled = bool(expected_balance_enabled)
    prefs.unusual_spending_enabled = bool(unusual_spending_enabled)
    prefs.unusual_spending_ai_enabled = bool(unusual_spending_ai_enabled)
    prefs.unusual_category_percent = int(unusual_category_percent)
    prefs.unusual_category_floor_minor = int(unusual_category_floor_minor)
    prefs.large_transaction_minor = large_transaction_minor
    prefs.save()
    changed = [
        label for label, names in (("alert_toggles", _ALERT_TOGGLES), ("thresholds", _ALERT_THRESHOLDS))
        if any(before[name] != getattr(prefs, name) for name in names)
    ]
    if changed:
        append_event(action=AuditEvent.Action.NOTIFICATION_PREFERENCES_CHANGED, actor=person,
                     target_id=prefs.pk, changed_fields=changed)
    return prefs


def current_household_members(household):
    if household is None:
        return []
    return list(
        Person.objects.filter(
            memberships__household=household,
            memberships__ended_at__isnull=True,
        ).distinct()
    )


def audience_for_account(account):
    if account.scope == Account.Scope.HOUSEHOLD:
        return current_household_members(account.household)
    return [account.owner]


def audience_for_budget(budget):
    if budget.scope == Budget.Scope.HOUSEHOLD:
        return current_household_members(budget.household)
    return [budget.owner]


def _budget_thresholds(spent_minor, available_minor):
    if available_minor <= 0:
        return spent_minor > 0, spent_minor > 0
    at_90 = spent_minor * 10 >= available_minor * 9
    at_100 = spent_minor >= available_minor
    return at_90, at_100


def evaluate_budget_alert(budget, *, today=None, recipient_cards=None):
    if budget.status != Budget.Status.ACTIVE:
        return []
    today = today or timezone.localdate()
    month = month_start(today)
    stamp = month.isoformat()[:7]
    month_label = f"{month_name[month.month]} {month.year}"
    link = f"{reverse('budgets')}?month={stamp}"
    created = []
    cards = recipient_cards if recipient_cards is not None else [
        (recipient, progress_snapshot(budget, month, recipient)) for recipient in audience_for_budget(budget)
    ]
    for recipient, card in cards:
        at_90, at_100 = _budget_thresholds(card.spent_minor, card.available_minor)
        if not at_90:
            continue
        created.extend(
            raise_alert(
                [recipient],
                Alert.Kind.BUDGET,
                f"{card.name} is at 90% of its {month_label} budget",
                link,
                f"budget:{budget.pk}:{stamp}:90",
            )
        )
        if at_100:
            created.extend(
                raise_alert(
                    [recipient],
                    Alert.Kind.BUDGET,
                    f"{card.name} reached its {month_label} budget",
                    link,
                    f"budget:{budget.pk}:{stamp}:100",
                )
            )
    return created


def evaluate_active_budget_alerts(*, today=None):
    today = today or timezone.localdate()
    created = []
    budgets = list(Budget.objects.filter(status=Budget.Status.ACTIVE).select_related(
        "category", "owner", "household"
    ).prefetch_related("amounts"))
    prefetch_related_objects([budget for budget in budgets if budget.rollover_enabled], "rollover_resets")
    groups = {}
    audiences = {}
    for budget in budgets:
        key = (budget.scope, budget.household_id if budget.scope == Budget.Scope.HOUSEHOLD else budget.owner_id)
        if key not in audiences:
            audiences[key] = audience_for_budget(budget)
        for recipient in audiences[key]:
            group = groups.setdefault(recipient.pk, {"person": recipient, "budgets": []})
            group["budgets"].append(budget)
    cards_by_budget = {}
    for group in groups.values():
        recipient = group["person"]
        for budget_id, card in progress_snapshots(group["budgets"], month_start(today), recipient).items():
            cards_by_budget.setdefault(budget_id, []).append((recipient, card))
    for budget in budgets:
        created.extend(evaluate_budget_alert(budget, today=today, recipient_cards=cards_by_budget.get(budget.pk, [])))
    return created


def raise_large_transaction_alerts(transactions):
    transactions = [txn for txn in transactions if txn.status == Transaction.Status.ACTIVE]
    accounts = {txn.account_id: txn.account for txn in transactions}
    people = {}
    audience = {}
    for account in accounts.values():
        audience[account.pk] = audience_for_account(account)
        people.update({person.pk: person for person in audience[account.pk]})
    preferences = {prefs.person_id: prefs for prefs in AlertSettings.objects.filter(person_id__in=people)}
    missing = [AlertSettings(person=person) for pk, person in people.items() if pk not in preferences]
    AlertSettings.objects.bulk_create(missing, ignore_conflicts=True)
    if missing:
        preferences = {prefs.person_id: prefs for prefs in AlertSettings.objects.filter(person_id__in=people)}
    pending = {}
    for txn in transactions:
        amount = abs(txn.amount_minor)
        link = reverse("transaction-edit", args=[txn.pk])
        title = f"Large transaction of {format_minor(amount, txn.currency)}"
        for person in audience[txn.account_id]:
            prefs = preferences[person.pk]
            threshold = prefs.large_transaction_minor
            if not prefs.large_transaction_enabled or threshold is None or threshold <= 0 or amount < threshold:
                continue
            key = (person.pk, f"large:{txn.pk}")
            pending[key] = Alert(
                recipient=person, kind=Alert.Kind.LARGE_TRANSACTION, title=title,
                link=link, dedupe_key=key[1], account=txn.account,
            )
    if not pending:
        return []
    existing = set(Alert.objects.filter(
        recipient_id__in=people, dedupe_key__in=[key[1] for key in pending],
    ).values_list("recipient_id", "dedupe_key"))
    created = [alert for key, alert in pending.items() if key not in existing]
    Alert.objects.bulk_create(created, ignore_conflicts=True)
    # ignore_conflicts does not populate ids; callers (including email notices)
    # need the persisted rows. Also retain deduplication on repeated passes.
    new_keys = {(alert.recipient_id, alert.dedupe_key) for alert in created}
    return [alert for alert in Alert.objects.filter(
        recipient_id__in=people, dedupe_key__in=[alert.dedupe_key for alert in created],
    ) if (alert.recipient_id, alert.dedupe_key) in new_keys]


def evaluate_recent_large_transactions(*, since=None):
    since = since or (timezone.now() - timedelta(days=1))
    rows = Transaction.objects.filter(
        status=Transaction.Status.ACTIVE,
        created_at__gte=since,
    ).select_related("account")
    return raise_large_transaction_alerts(rows)


def sync_needs_attention(connection):
    if connection.disabled:
        return "relink"
    result = connection.last_sync_result or ""
    if not result or result.startswith("Synced "):
        return None
    return "failed"


def raise_sync_alert(connection):
    reason = sync_needs_attention(connection)
    if reason is None:
        return []
    title = (
        "A SimpleFIN connection needs re-linking"
        if reason == "relink"
        else "A SimpleFIN sync failed"
    )
    day = timezone.localdate().isoformat()
    return raise_alert(
        [connection.owner],
        Alert.Kind.SYNC,
        title,
        reverse("simplefin-connections"),
        f"sync:{connection.pk}:{day}",
    )


def evaluate_sync_alerts():
    created = []
    for connection in SimpleFinConnection.objects.select_related("owner"):
        created.extend(raise_sync_alert(connection))
    return created


def after_new_transactions(transactions):
    created = raise_large_transaction_alerts(transactions)
    created.extend(evaluate_active_budget_alerts())
    return created


def after_category_change():
    return evaluate_active_budget_alerts()


def schedule_after_category_change():
    transaction.on_commit(after_category_change)


def schedule_after_new_transactions(transactions):
    pks = [txn.pk for txn in transactions]

    def _run():
        rows = list(Transaction.objects.filter(pk__in=pks).select_related("account", "account__owner", "account__household"))
        after_new_transactions(rows)

    transaction.on_commit(_run)


def purge_old_read_alerts(*, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=READ_RETENTION_DAYS)
    deleted, _detail = Alert.objects.filter(read_at__isnull=False, created_at__lt=cutoff).delete()
    return deleted


@notify_after_alert_run
def run_daily_alert_pass(*, today=None, now=None):
    created = evaluate_sync_alerts()
    created.extend(evaluate_active_budget_alerts(today=today))
    created.extend(evaluate_recent_large_transactions())
    from .backup_health import evaluate_backup_alerts
    from .monthly_review import generate_due_monthly_reviews

    generate_due_monthly_reviews(today=today)
    created.extend(evaluate_backup_alerts(today=today, now=now))
    from .bills_calendar import evaluate_expected_balance_alerts

    created.extend(evaluate_expected_balance_alerts(today=today))
    purge_old_read_alerts(now=now)
    from .receipt_services import sweep_orphan_receipt_files
    from .security_services import (
        purge_old_security_events,
        purge_stale_member_sessions,
    )

    purge_old_security_events(now=now)
    purge_stale_member_sessions(now=now)
    sweep_orphan_receipt_files(now=now)
    return created
