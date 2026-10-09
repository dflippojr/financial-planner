"""Computed monthly review facts for a closed month, stored per member."""

from calendar import month_name
from datetime import date
from hashlib import sha256
from urllib.parse import urlencode

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .audit_operations import execution, operation, outcome

from .alert_services import raise_alert, settings_for
from .budget_services import month_budget_cards
from .cash_flow import (
    GROUPING_MONTH,
    cash_flow_report,
    format_minor,
    spending_by_category_report,
    spending_category_detail_url,
)
from .category_services import income_and_spending_totals
from .models import (
    Account,
    Alert,
    MonthlyReview,
    Person,
    RecurringSeries,
    SavingsGoal,
    Transaction,
    TransferPair,
)
from .months import add_months, month_end, month_start
from .net_worth import net_worth_report
from .recurring_review import build_recurring_review
from .savings_goal_services import SOURCE_NONE, goal_progress


def latest_closed_month(today=None):
    today = today or timezone.localdate()
    return add_months(month_start(today), -1)


def parse_review_month(raw, *, today=None):
    today = today or timezone.localdate()
    closed = latest_closed_month(today)
    if not raw:
        return closed
    try:
        year_s, month_s = raw.split("-", 1)
        parsed = date(int(year_s), int(month_s), 1)
    except (TypeError, ValueError, AttributeError):
        return closed
    if parsed > closed:
        return closed
    return parsed


def visibility_key(principal):
    ids = Account.objects.visible_to(principal).order_by("pk").values_list("pk", flat=True)
    payload = ",".join(str(pk) for pk in ids)
    return sha256(payload.encode()).hexdigest()


def _month_query(start, end):
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "grouping": GROUPING_MONTH}


def _cash_flow_url(start, end):
    return f"{reverse('home')}?{urlencode(_month_query(start, end))}"


def _spending_url(start, end):
    return f"{reverse('spending-by-category')}?{urlencode(_month_query(start, end))}"


def _net_worth_url(start, end):
    query = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    return f"{reverse('net-worth')}?{urlencode(query)}"


def _review_url(month):
    return f"{reverse('monthly-review')}?month={month_start(month).isoformat()[:7]}"


def _totals(principal, start, end):
    accounts = list(Account.objects.visible_to(principal).for_cash_flow())
    return income_and_spending_totals(
        principal, date_from=start, date_to=end, accounts=accounts
    )


def _change_block(current, previous):
    if previous is None:
        return {
            "income_minor": None,
            "spending_minor": None,
            "net_minor": None,
            "income_display": "—",
            "spending_display": "—",
            "net_display": "—",
            "income_delta_minor": None,
            "spending_delta_minor": None,
            "net_delta_minor": None,
            "income_delta_display": "—",
            "spending_delta_display": "—",
            "net_delta_display": "—",
        }
    return {
        "income_minor": previous.income_minor,
        "spending_minor": previous.spending_minor,
        "net_minor": previous.net_minor,
        "income_display": format_minor(previous.income_minor),
        "spending_display": format_minor(previous.spending_minor),
        "net_display": format_minor(previous.net_minor),
        "income_delta_minor": current.income_minor - previous.income_minor,
        "spending_delta_minor": current.spending_minor - previous.spending_minor,
        "net_delta_minor": current.net_minor - previous.net_minor,
        "income_delta_display": format_minor(current.income_minor - previous.income_minor),
        "spending_delta_display": format_minor(current.spending_minor - previous.spending_minor),
        "net_delta_display": format_minor(current.net_minor - previous.net_minor),
    }


def _cash_flow_facts(principal, start, end, current):
    prior_start = add_months(start, -1)
    prior_end = month_end(prior_start)
    year_start = add_months(start, -12)
    year_end = month_end(year_start)
    prior = _totals(principal, prior_start, prior_end)
    year_ago = _totals(principal, year_start, year_end)
    report = cash_flow_report(
        principal,
        date_from=start,
        date_to=end,
        grouping=GROUPING_MONTH,
        today=end,
    )
    missing = bool(report.periods) and report.periods[0].missing_import
    return {
        "income_minor": current.income_minor,
        "spending_minor": current.spending_minor,
        "net_minor": current.net_minor,
        "income_display": format_minor(current.income_minor),
        "spending_display": format_minor(current.spending_minor),
        "net_display": format_minor(current.net_minor),
        "prior": _change_block(current, prior),
        "year_ago": _change_block(current, year_ago),
        "cash_flow_url": _cash_flow_url(start, end),
        "missing_import": missing,
        "missing_import_url": _cash_flow_url(start, end),
    }


def _category_rows(principal, start, end):
    report = spending_by_category_report(principal, date_from=start, date_to=end)
    return {row.key: row for row in report.rows}


def _category_delta_item(key, current_row, previous_row, start, end, delta):
    name = current_row.name if current_row is not None else previous_row.name
    filter_value = current_row.key if current_row is not None else previous_row.key
    current_minor = current_row.spending_minor if current_row is not None else 0
    previous_minor = previous_row.spending_minor if previous_row is not None else 0
    return {
        "key": key,
        "name": name,
        "current_minor": current_minor,
        "previous_minor": previous_minor,
        "delta_minor": delta,
        "current_display": format_minor(current_minor),
        "previous_display": format_minor(previous_minor),
        "delta_display": format_minor(delta),
        "url": spending_category_detail_url(filter_value, start, end),
    }


def _category_facts(principal, start, end):
    current_rows = _category_rows(principal, start, end)
    prior_start = add_months(start, -1)
    prior_end = month_end(prior_start)
    prior_rows = _category_rows(principal, prior_start, prior_end)
    keys = set(current_rows) | set(prior_rows)
    deltas = []
    for key in keys:
        current_row = current_rows.get(key)
        previous_row = prior_rows.get(key)
        current_minor = current_row.spending_minor if current_row is not None else 0
        previous_minor = previous_row.spending_minor if previous_row is not None else 0
        delta = current_minor - previous_minor
        if delta == 0:
            continue
        deltas.append(_category_delta_item(key, current_row, previous_row, start, end, delta))
    increases = sorted(
        [item for item in deltas if item["delta_minor"] > 0],
        key=lambda item: (-item["delta_minor"], item["name"]),
    )[:3]
    decreases = sorted(
        [item for item in deltas if item["delta_minor"] < 0],
        key=lambda item: (item["delta_minor"], item["name"]),
    )[:3]
    return {
        "category_increases": increases,
        "category_decreases": decreases,
        "spending_url": _spending_url(start, end),
    }


def _recurring_item(series, **extra):
    row = {
        "series_id": series.pk,
        "name": series.display_name,
        "amount_display": series.amount_display,
        "url": reverse("recurring-review"),
    }
    row.update(extra)
    return row


def _in_month(value, start, end):
    if value is None:
        return False
    if hasattr(value, "date"):
        value = timezone.localdate(value)
    return start <= value <= end


def _recurring_facts(principal, start, end, today):
    visible = list(
        RecurringSeries.objects.visible_to(principal).prefetch_related("members__transaction__account")
    )
    _upcoming, changes, missed, cancelled = build_recurring_review(principal, visible, today=today)
    price_rows = [
        _recurring_item(
            change.series,
            previous_display=change.previous_display,
            new_display=change.new_display,
            percent_display=change.percent_display,
            charge_date=change.charge_date.isoformat(),
        )
        for change in changes
        if _in_month(change.charge_date, start, end)
    ]
    missed_rows = [
        _recurring_item(
            item.series,
            expected_on=item.expected_on.isoformat(),
            missing_import=item.missing_import,
        )
        for item in missed
        if _in_month(item.expected_on, start, end)
    ]
    cancel_rows = [
        _recurring_item(item.series, cancelled_on=timezone.localdate(item.series.cancelled_at).isoformat())
        for item in cancelled
        if _in_month(item.series.cancelled_at, start, end)
    ]
    new_rows = [
        _recurring_item(series)
        for series in visible
        if series.status == RecurringSeries.Status.CONFIRMED and _in_month(series.confirmed_at, start, end)
    ]
    new_rows.sort(key=lambda item: item["name"])
    return {
        "price_changes": price_rows,
        "missed_charges": missed_rows,
        "cancellations": cancel_rows,
        "new_recurring": new_rows,
        "recurring_url": reverse("recurring-review"),
    }


def _budget_facts(principal, start):
    cards = month_budget_cards(principal, start)
    over_rows = [
        {
            "name": card.name,
            "spent_display": card.spent_display,
            "over_by_display": card.over_by_display,
            "url": f"{reverse('budgets')}?month={start.isoformat()[:7]}",
        }
        for card in cards
        if card.over_budget
    ]
    remaining = [card for card in cards if not card.over_budget]
    largest = None
    if remaining:
        pick = max(remaining, key=lambda card: (card.remaining_minor, card.name))
        largest = {
            "name": pick.name,
            "remaining_display": pick.remaining_display,
            "url": f"{reverse('budgets')}?month={start.isoformat()[:7]}",
        }
    return {
        "budgets_over": over_rows,
        "largest_remaining": largest,
        "budgets_url": f"{reverse('budgets')}?month={start.isoformat()[:7]}",
    }


def _goal_facts(principal, today):
    goals = SavingsGoal.objects.visible_to(principal).filter(status=SavingsGoal.Status.ACTIVE)
    rows = []
    for goal in goals.order_by("name", "pk"):
        card = goal_progress(principal, goal, today=today)
        if card.source == SOURCE_NONE:
            rows.append(
                {
                    "name": goal.name,
                    "percent": None,
                    "remaining_display": None,
                    "current_display": None,
                    "target_display": card.target_amount_display,
                    "url": reverse("savings-goals"),
                }
            )
            continue
        rows.append(
            {
                "name": goal.name,
                "percent": card.percent,
                "remaining_display": card.remaining_display,
                "current_display": card.current_amount_display,
                "target_display": card.target_amount_display,
                "url": reverse("savings-goals"),
            }
        )
    return {"savings_goals": rows, "goals_url": reverse("savings-goals")}


def _net_worth_facts(principal, start, end):
    prior_start = add_months(start, -1)
    report = net_worth_report(
        principal,
        date_from=prior_start,
        date_to=end,
        today=end,
    )
    current_period = next((period for period in report.periods if period.start == start), None)
    prior_period = next((period for period in report.periods if period.start == prior_start), None)
    current_minor = current_period.net_minor if current_period is not None else 0
    prior_minor = prior_period.net_minor if prior_period is not None else None
    change_minor = None if prior_minor is None else current_minor - prior_minor
    return {
        "net_worth_minor": current_minor,
        "net_worth_display": format_minor(current_minor),
        "net_worth_prior_minor": prior_minor,
        "net_worth_prior_display": "—" if prior_minor is None else format_minor(prior_minor),
        "net_worth_change_minor": change_minor,
        "net_worth_change_display": "—" if change_minor is None else format_minor(change_minor),
        "net_worth_url": _net_worth_url(start, end),
    }


def _excluded_transfer_ids(principal):
    return {
        tx_id
        for pair in TransferPair.objects.excluding_income_and_spending().visible_to(principal)
        for tx_id in (pair.leg_a_id, pair.leg_b_id)
    }


def _large_transaction_facts(principal, start, end):
    from .models import _person_for

    person = _person_for(principal)
    prefs = settings_for(person)
    threshold = prefs.large_transaction_minor
    excluded = _excluded_transfer_ids(principal)
    rows = list(
        Transaction.objects.visible_to(principal)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            transaction_date__gte=start,
            transaction_date__lte=end,
        )
        .select_related("account")
    )
    rows = [txn for txn in rows if txn.pk not in excluded]
    rows.sort(key=lambda txn: (-abs(txn.amount_minor), txn.transaction_date, txn.pk))
    if threshold is not None and threshold > 0:
        rows = [txn for txn in rows if abs(txn.amount_minor) >= threshold]
    picked = rows[:3]
    return {
        "large_transactions": [
            {
                "description": txn.description,
                "amount_display": format_minor(txn.amount_minor, txn.currency),
                "date": txn.transaction_date.isoformat(),
                "url": reverse("transaction-edit", args=[txn.pk]),
            }
            for txn in picked
        ]
    }


def compute_monthly_review_facts(principal, month, *, today=None):
    """Facts for one closed month, using the viewer's current visible accounts."""
    start = month_start(month)
    end = month_end(start)
    today = today or timezone.localdate()
    current = _totals(principal, start, end)
    facts = {
        "month": start.isoformat()[:7],
        "month_label": f"{month_name[start.month]} {start.year}",
    }
    facts.update(_cash_flow_facts(principal, start, end, current))
    facts.update(_category_facts(principal, start, end))
    facts.update(_recurring_facts(principal, start, end, today))
    facts.update(_budget_facts(principal, start))
    facts.update(_goal_facts(principal, end))
    facts.update(_net_worth_facts(principal, start, end))
    facts.update(_large_transaction_facts(principal, start, end))
    from .unusual_spending import compute_unusual_flags, unusual_settings_signature

    flags = compute_unusual_flags(principal, start)
    facts["unusual"] = list(flags)
    facts["unusual_omitted_count"] = getattr(flags, "omitted_count", 0)
    facts["unusual_settings"] = unusual_settings_signature(principal)
    return facts


def _raise_review_alert(person, month):
    month = month_start(month)
    title = f"Your {month_name[month.month]} review is ready"
    return raise_alert(
        [person],
        Alert.Kind.MONTHLY_REVIEW,
        title,
        _review_url(month),
        f"review:{month.isoformat()[:7]}",
    )


@transaction.atomic
def store_monthly_review(principal, month, *, force=False, today=None, raise_inbox=True):
    from .models import _person_for

    person = _person_for(principal)
    month = month_start(month)
    today = today or timezone.localdate()
    key = visibility_key(person)
    existing = MonthlyReview.objects.filter(person=person, month=month).first()
    from .unusual_spending import unusual_settings_signature

    if (
        existing is not None
        and not force
        and existing.visibility_key == key
        and "unusual" in (existing.facts or {})
        and "unusual_omitted_count" in (existing.facts or {})
        and (existing.facts or {}).get("unusual_settings") == unusual_settings_signature(person)
    ):
        return existing, False
    facts = compute_monthly_review_facts(person, month, today=today)
    now = timezone.now()
    review, _created = MonthlyReview.objects.update_or_create(
        person=person,
        month=month,
        defaults={
            "visibility_key": key,
            "facts": facts,
            "generated_at": now,
            "ai_paragraph": "",
            "ai_backend": "",
            "unusual_ai_paragraph": "",
            "unusual_ai_backend": "",
        },
    )
    from .monthly_review_ai import queue_monthly_review_phrasing
    from .unusual_spending import raise_unusual_alerts
    from .unusual_spending_ai import queue_unusual_phrasing

    queue_monthly_review_phrasing(person, review)
    queue_unusual_phrasing(person, review)
    if raise_inbox:
        _raise_review_alert(person, month)
        raise_unusual_alerts(person, month, facts.get("unusual") or [])
    outcome(person, "monthly_review", review.pk)
    return review, True


def review_for_viewer(principal, month, *, today=None, force=False):
    return store_monthly_review(principal, month, force=force, today=today, raise_inbox=True)[0]


def generate_due_monthly_reviews(*, today=None):
    today = today or timezone.localdate()
    month = latest_closed_month(today)
    created = []
    for person in Person.objects.order_by("pk"):
        if execution.get() is None:
            with operation():
                review, wrote = store_monthly_review(person, month, force=False, today=today)
        else:
            review, wrote = store_monthly_review(person, month, force=False, today=today)
        if wrote:
            created.append(review)
    return created
