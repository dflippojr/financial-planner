import logging
from calendar import month_name
from datetime import timedelta
from functools import partial

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import CharField, Exists, F, Max, Min, OuterRef, Q, Value, prefetch_related_objects
from django.db.models.functions import Cast, Concat
from django.urls import reverse
from django.utils import timezone

from .access import DENIED as _DENIED
from .alert_email import notify_after_alert_run
from .audit_services import append_event
from .audit_operations import journal_run, outcome, scheduled_operation
from .budget_services import progress_snapshot, progress_snapshots
from .cash_flow import format_minor
from .input_limits import MAX_LARGE_ALERTS
from .models import _person_for
from .models import (
    AuditEvent,
    Account,
    Alert,
    AlertSettings,
    Budget,
    Person,
    SimpleFinConnection,
    Transaction,
)
from .months import month_start
from .pass_isolation import for_each_member, run_step

logger = logging.getLogger(__name__)

_KIND_ENABLED_FIELD = {
    Alert.Kind.SYNC: "sync_enabled",
    Alert.Kind.RECURRING_PRICE: "recurring_price_enabled",
    Alert.Kind.RECURRING_MISSED: "recurring_missed_enabled",
    Alert.Kind.BUDGET: "budget_enabled",
    Alert.Kind.LARGE_TRANSACTION: "large_transaction_enabled",
    Alert.Kind.MONTHLY_REVIEW: "monthly_review_enabled",
    Alert.Kind.EXPECTED_BALANCE: "expected_balance_enabled",
    Alert.Kind.IMPORT_DUE: "import_due_enabled",
    Alert.Kind.UNUSUAL_SPENDING: "unusual_spending_enabled",
}
READ_RETENTION_DAYS = 180
UNREAD_RETENTION_DAYS = 180


def settings_for(person):
    prefs, _created = AlertSettings.objects.get_or_create(person=person)
    return prefs


def kind_is_enabled(prefs, kind):
    if kind in (Alert.Kind.BACKUP, Alert.Kind.RECONCILIATION):
        return True
    field = _KIND_ENABLED_FIELD.get(kind)
    return bool(field and getattr(prefs, field))


def _internal_link(link):
    return link.startswith("/") and not link.startswith("//")


@transaction.atomic
@scheduled_operation
def raise_alert(recipients, kind, title, link, dedupe_key, account=None, source_accounts=()):
    """Create one inbox row per recipient unless that condition was already raised.

    source_accounts are every account the title quotes data from; a recipient
    who later loses sight of any of them no longer sees the alert.
    """
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
            # Sources are the visibility snapshot of the first raise; a later
            # repeat must not widen it with accounts the original never quoted.
            if source_accounts:
                alert.source_accounts.add(*source_accounts)
            created.append(alert)
            outcome(recipient, "alert_delivery", alert.pk, account=account)
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
    visible_budgets = Budget.objects.visible_to(person).annotate(
        _alert_key=Cast(OuterRef("dedupe_key"), output_field=CharField()),
        _key=Concat(Value("budget:"), Cast("pk", output_field=CharField())),
        _prefix=Concat(Value("budget:"), Cast("pk", output_field=CharField()), Value(":")),
    ).filter(Q(_alert_key=F("_key")) | Q(_alert_key__startswith=F("_prefix")))
    # An alert quoting an account the viewer can no longer see (after an
    # unshare or leaving the household) drops out of their view.
    hidden_sources = Alert.source_accounts.through.objects.filter(alert_id=OuterRef("pk")).exclude(
        account_id__in=visible_accounts
    )
    return (
        Alert.objects.filter(recipient=person)
        .exclude(kind=Alert.Kind.LARGE_TRANSACTION, account_id__isnull=True)
        .filter(Q(account_id__isnull=True) | Q(account_id__in=visible_accounts))
        .alias(_budget_visible=Exists(visible_budgets))
        .filter(~Q(kind=Alert.Kind.BUDGET) | Q(_budget_visible=True))
        .exclude(Exists(hidden_sources))
        .order_by("-created_at", "-pk")
    )


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
    "expected_balance_enabled", "unusual_spending_enabled", "unusual_spending_ai_enabled", "import_due_enabled",
)
_ALERT_THRESHOLDS = (
    "unusual_category_percent", "unusual_category_floor_minor", "large_transaction_minor", "recurring_price_percent",
)


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
    recurring_price_percent=10,
    import_due_enabled=True,
):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    prefs = settings_for(person)
    before = {name: getattr(prefs, name) for name in (*_ALERT_TOGGLES, *_ALERT_THRESHOLDS)}
    prefs.sync_enabled = bool(sync_enabled)
    prefs.recurring_price_enabled = bool(recurring_price_enabled)
    prefs.recurring_price_percent = int(recurring_price_percent)
    prefs.recurring_missed_enabled = bool(recurring_missed_enabled)
    prefs.budget_enabled = bool(budget_enabled)
    prefs.large_transaction_enabled = bool(large_transaction_enabled)
    prefs.monthly_review_enabled = bool(monthly_review_enabled)
    prefs.monthly_review_ai_enabled = bool(monthly_review_ai_enabled)
    prefs.expected_balance_enabled = bool(expected_balance_enabled)
    prefs.import_due_enabled = bool(import_due_enabled)
    prefs.unusual_spending_enabled = bool(unusual_spending_enabled)
    prefs.unusual_spending_ai_enabled = bool(unusual_spending_ai_enabled)
    prefs.unusual_category_percent = int(unusual_category_percent)
    prefs.unusual_category_floor_minor = int(unusual_category_floor_minor)
    prefs.large_transaction_minor = large_transaction_minor
    prefs.save(update_fields=(*_ALERT_TOGGLES, *_ALERT_THRESHOLDS))
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
    if not transactions:
        return []
    rows = [(txn.pk, txn.account_id, txn.amount_minor, txn.currency) for txn in transactions]
    accounts = {txn.account_id: txn.account for txn in transactions}
    return _raise_large_alerts(rows, accounts, lambda: (transactions[0].pk, transactions[-1].pk))


def _smallest_large_threshold():
    """The lowest enabled positive threshold any member has, or None.

    Members without a settings row have no threshold, so they cannot receive
    a large-transaction alert and need not be considered.
    """
    return AlertSettings.objects.filter(
        large_transaction_enabled=True, large_transaction_minor__gt=0,
    ).aggregate(smallest=Min("large_transaction_minor"))["smallest"]


def _raise_large_alerts_for(rows):
    """Raise large-transaction alerts for active rows of a queryset.

    Only rows that could cross some member's threshold are read, as plain
    values; rows below every threshold never become model instances.
    """
    smallest = _smallest_large_threshold()
    if smallest is None:
        return []
    rows = rows.filter(status=Transaction.Status.ACTIVE).order_by("pk")
    candidates = list(rows.filter(
        Q(amount_minor__gte=smallest) | Q(amount_minor__lte=-smallest),
    ).values_list("pk", "account_id", "amount_minor", "currency"))
    if not candidates:
        return []
    accounts = Account.objects.filter(
        pk__in={account_id for _pk, account_id, _amount, _currency in candidates},
    ).select_related("owner", "household").in_bulk()

    def bounds():
        # The summary key names the whole run, not just the candidate rows.
        span = rows.aggregate(first=Min("pk"), last=Max("pk"))
        return span["first"], span["last"]

    return _raise_large_alerts(candidates, accounts, bounds)


def _raise_large_alerts(rows, accounts, bounds):
    """rows are (pk, account_id, amount_minor, currency) tuples in run order;
    bounds() returns the first and last pk of the whole run for summary keys."""
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
    counts = {}
    omitted = {}
    for pk, account_id, amount_minor, currency in rows:
        amount = abs(amount_minor)
        for person in audience[account_id]:
            prefs = preferences[person.pk]
            threshold = prefs.large_transaction_minor
            if not prefs.large_transaction_enabled or threshold is None or threshold <= 0 or amount < threshold:
                continue
            key = (person.pk, f"large:{pk}")
            if key in pending:
                continue
            if counts.get(person.pk, 0) >= MAX_LARGE_ALERTS:
                omitted.setdefault((person.pk, account_id), True)
                continue
            counts[person.pk] = counts.get(person.pk, 0) + 1
            pending[key] = Alert(
                recipient=person, kind=Alert.Kind.LARGE_TRANSACTION,
                title=f"Large transaction of {format_minor(amount, currency)}",
                link=reverse("transaction-edit", args=[pk]), dedupe_key=key[1], account=accounts[account_id],
            )
    # One summary per recipient; it is tied to an account so revoked access
    # cannot leave an accountless large-transaction alert visible.
    summarized = set()
    span = bounds() if omitted else None
    for person_id, account_id in omitted:
        if person_id in summarized:
            continue
        summarized.add(person_id)
        key = (person_id, f"large-summary:{account_id}:{span[0]}:{span[1]}")
        pending[key] = Alert(
            recipient=people[person_id], kind=Alert.Kind.LARGE_TRANSACTION,
            title="More large transactions are available in your transaction list",
            link=f"{reverse('transaction-list')}?account={account_id}",
            dedupe_key=key[1], account=accounts[account_id],
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
    return _raise_large_alerts_for(Transaction.objects.filter(created_at__gte=since))


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


def after_new_transactions(rows):
    """Run alerts for a queryset of newly created transactions."""
    created = _raise_large_alerts_for(rows)
    created.extend(evaluate_active_budget_alerts())
    return created


def after_category_change():
    return evaluate_active_budget_alerts()


def schedule_after_category_change():
    transaction.on_commit(after_category_change)


def schedule_after_new_transactions(transactions):
    pks = [txn.pk for txn in transactions]

    def _run():
        after_new_transactions(Transaction.objects.filter(pk__in=pks))

    transaction.on_commit(_run)


def purge_old_read_alerts(*, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=READ_RETENTION_DAYS)
    unread_cutoff = now - timedelta(days=UNREAD_RETENTION_DAYS)
    deleted, _detail = Alert.objects.filter(
        Q(read_at__isnull=False, created_at__lt=cutoff)
        | Q(read_at__isnull=True, created_at__lt=unread_cutoff)
    ).delete()
    return deleted


def refresh_all_recurring_series(*, today=None):
    """Bring every active member's series in line and raise their review alerts.

    Each member runs in its own transaction, so one failure (a lock timeout,
    say) rolls back only that member and the pass moves on.
    """
    from .recurring_services import refresh_recurring_series

    for_each_member(
        Person.objects.filter(user__is_active=True).order_by("pk"),
        lambda person: refresh_recurring_series(person, only_if_changed=True, today=today),
        label="Daily recurring refresh",
        logger=logger,
    )


class DailyAlertPassIncomplete(Exception):
    """One or more steps of the daily alert pass failed; every other step still ran."""

    def __init__(self, steps):
        self.steps = tuple(steps)
        super().__init__(f"Daily alert pass steps failed: {', '.join(self.steps)}")


@notify_after_alert_run
@journal_run("daily_alerts")
def run_daily_alert_pass(*, today=None, now=None):
    """Run every daily alert and retention step, each isolated from the others.

    A failing step is logged by name and error type only, and the pass moves
    on. If any step failed, raises DailyAlertPassIncomplete at the end so the
    daily_alerts journal records a failure. A database-unavailable error stops
    the pass at once.
    """
    from .auth_services import purge_expired_login_throttles
    from .backup_health import evaluate_backup_alerts
    from .bills_calendar import evaluate_expected_balance_alerts
    from .import_due import evaluate_import_due_alerts
    from .monthly_review import generate_due_monthly_reviews
    from .passkey_services import purge_stale_pending_sign_ins
    from .receipt_services import sweep_orphan_receipt_files
    from .reconciliation import evaluate_reconciliation_alerts
    from .security_services import purge_expired_sessions, purge_old_security_events, purge_stale_member_sessions

    # (name, function, whether it returns new alerts).
    steps = (
        ("sync_alerts", evaluate_sync_alerts, True),
        # Before the monthly review and expected-balance steps, which read series.
        ("recurring_refresh", partial(refresh_all_recurring_series, today=today), False),
        ("budget_alerts", partial(evaluate_active_budget_alerts, today=today), True),
        ("large_transaction_alerts", evaluate_recent_large_transactions, True),
        ("monthly_reviews", partial(generate_due_monthly_reviews, today=today), False),
        ("backup_alerts", partial(evaluate_backup_alerts, today=today, now=now), True),
        ("expected_balance_alerts", partial(evaluate_expected_balance_alerts, today=today), True),
        ("import_due_alerts", partial(evaluate_import_due_alerts, today=today), True),
        ("reconciliation_alerts", partial(evaluate_reconciliation_alerts, now=now), True),
        ("purge_read_alerts", partial(purge_old_read_alerts, now=now), False),
        ("purge_security_events", partial(purge_old_security_events, now=now), False),
        ("purge_login_throttles", partial(purge_expired_login_throttles, now=now), False),
        ("purge_expired_sessions", partial(purge_expired_sessions, now=now), False),
        ("purge_stale_member_sessions", partial(purge_stale_member_sessions, now=now), False),
        ("purge_pending_sign_ins", partial(purge_stale_pending_sign_ins, now=now), False),
        ("receipt_sweep", partial(sweep_orphan_receipt_files, now=now), False),
    )
    created = []
    failed = []
    for name, function, returns_alerts in steps:
        result = run_step(name, function, failed=failed, logger=logger)
        if returns_alerts and result is not None:
            created.extend(result)
    if failed:
        raise DailyAlertPassIncomplete(failed)
    return created
