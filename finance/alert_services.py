from calendar import month_name
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .budget_services import month_start, progress_snapshot
from .cash_flow import format_minor
from .models import (
    Account,
    Alert,
    AlertSettings,
    Budget,
    Person,
    SimpleFinConnection,
    Transaction,
)

_DENIED = "Operation is not permitted."
_KIND_ENABLED_FIELD = {
    Alert.Kind.SYNC: "sync_enabled",
    Alert.Kind.RECURRING_PRICE: "recurring_price_enabled",
    Alert.Kind.RECURRING_MISSED: "recurring_missed_enabled",
    Alert.Kind.BUDGET: "budget_enabled",
    Alert.Kind.LARGE_TRANSACTION: "large_transaction_enabled",
    Alert.Kind.MONTHLY_REVIEW: "monthly_review_enabled",
}
READ_RETENTION_DAYS = 180


def _person_for(principal):
    from .models import _person_for as resolve_person

    return resolve_person(principal)


def settings_for(person):
    prefs, _created = AlertSettings.objects.get_or_create(person=person)
    return prefs


def kind_is_enabled(prefs, kind):
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
):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    prefs = settings_for(person)
    prefs.sync_enabled = bool(sync_enabled)
    prefs.recurring_price_enabled = bool(recurring_price_enabled)
    prefs.recurring_missed_enabled = bool(recurring_missed_enabled)
    prefs.budget_enabled = bool(budget_enabled)
    prefs.large_transaction_enabled = bool(large_transaction_enabled)
    prefs.monthly_review_enabled = bool(monthly_review_enabled)
    prefs.monthly_review_ai_enabled = bool(monthly_review_ai_enabled)
    prefs.large_transaction_minor = large_transaction_minor
    prefs.save()
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


def evaluate_budget_alert(budget, *, today=None):
    if budget.status != Budget.Status.ACTIVE:
        return []
    today = today or timezone.localdate()
    month = month_start(today)
    stamp = month.isoformat()[:7]
    month_label = f"{month_name[month.month]} {month.year}"
    link = f"{reverse('budgets')}?month={stamp}"
    created = []
    for recipient in audience_for_budget(budget):
        card = progress_snapshot(budget, month, recipient)
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
    created = []
    budgets = Budget.objects.filter(status=Budget.Status.ACTIVE).select_related(
        "category", "owner", "household"
    )
    for budget in budgets:
        created.extend(evaluate_budget_alert(budget, today=today))
    return created


def raise_large_transaction_alerts(transactions):
    created = []
    for txn in transactions:
        if txn.status != Transaction.Status.ACTIVE:
            continue
        amount = abs(txn.amount_minor)
        link = reverse("transaction-edit", args=[txn.pk])
        title = f"Large transaction of {format_minor(amount, txn.currency)}"
        for person in audience_for_account(txn.account):
            prefs = settings_for(person)
            threshold = prefs.large_transaction_minor
            if threshold is None or threshold <= 0 or amount < threshold:
                continue
            created.extend(
                raise_alert(
                    [person],
                    Alert.Kind.LARGE_TRANSACTION,
                    title,
                    link,
                    f"large:{txn.pk}",
                    account=txn.account,
                )
            )
    return created


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
        rows = list(Transaction.objects.filter(pk__in=pks).select_related("account"))
        after_new_transactions(rows)

    transaction.on_commit(_run)


def purge_old_read_alerts(*, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=READ_RETENTION_DAYS)
    deleted, _detail = Alert.objects.filter(read_at__isnull=False, created_at__lt=cutoff).delete()
    return deleted


def run_daily_alert_pass(*, today=None, now=None):
    created = evaluate_sync_alerts()
    created.extend(evaluate_active_budget_alerts(today=today))
    created.extend(evaluate_recent_large_transactions())
    from .monthly_review import generate_due_monthly_reviews

    generate_due_monthly_reviews(today=today)
    purge_old_read_alerts(now=now)
    return created
