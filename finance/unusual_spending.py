"""Deterministic unusual-spending flags from visible cash-flow facts."""

from hashlib import sha256
from heapq import heappop, heappush
from decimal import ROUND_HALF_EVEN, Decimal

from django.urls import reverse

from .alert_services import raise_alert, settings_for
from .cash_flow import (
    format_minor,
    selected_accounts,
    spending_by_category_report,
    spending_category_detail_url,
)
from .models import _person_for
from .models import Account, Alert, Transaction, TransferPair
from .months import add_months, month_end, month_start
from .recurring_services import merchant_key

KIND_CATEGORY = "category"
KIND_MERCHANT = "merchant"
KIND_NEW_MERCHANT = "new_merchant"
DEFAULT_CATEGORY_PERCENT = 50
DEFAULT_CATEGORY_FLOOR_MINOR = 5_000
MERCHANT_MULTIPLIER = Decimal("2")
MERCHANT_MIN_PRIOR = 3
BASELINE_MONTHS = 6


def _excluded_transfer_ids(principal):
    return {
        tx_id
        for pair in TransferPair.objects.excluding_income_and_spending().visible_to(principal)
        for tx_id in (pair.leg_a_id, pair.leg_b_id)
    }


def _category_thresholds(prefs):
    percent = getattr(prefs, "unusual_category_percent", None)
    floor = getattr(prefs, "unusual_category_floor_minor", None)
    if percent is None:
        percent = DEFAULT_CATEGORY_PERCENT
    if floor is None:
        floor = DEFAULT_CATEGORY_FLOOR_MINOR
    return int(percent), int(floor)


def unusual_settings_signature(principal):
    """The settings a month's flags depend on; a stored review is rebuilt when they change."""
    prefs = settings_for(_person_for(principal))
    percent, floor = _category_thresholds(prefs)
    return [percent, floor, prefs.large_transaction_minor]


def _whole_minor(value):
    # Stored as an integer like every other *_minor fact, so AI grounding reads it as cents.
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _median_minor(values):
    ordered = sorted(int(value) for value in values)
    count = len(ordered)
    if count == 0:
        return Decimal("0")
    middle = count // 2
    if count % 2:
        return Decimal(ordered[middle])
    return (Decimal(ordered[middle - 1]) + Decimal(ordered[middle])) / Decimal("2")


def exceeds_category_baseline(current_minor, baseline_minor, percent, floor_minor):
    if current_minor <= 0:
        return False
    baseline = Decimal(baseline_minor)
    current = Decimal(current_minor)
    ratio = Decimal(percent) / Decimal("100")
    return current >= baseline * (Decimal("1") + ratio) and current >= baseline + Decimal(floor_minor)


def exceeds_merchant_median(amount_minor, median_minor, *, prior_count):
    if prior_count < MERCHANT_MIN_PRIOR or median_minor <= 0 or amount_minor <= 0:
        return False
    return Decimal(amount_minor) > MERCHANT_MULTIPLIER * Decimal(median_minor)


def _row_spending(report, key):
    for row in report.rows:
        if row.key == key:
            return row.spending_minor
    return 0


def _category_flags(principal, start, end, prefs, *, account=None, scope="", tag=None, accounts=None):
    percent, floor_minor = _category_thresholds(prefs)
    current_report = spending_by_category_report(
        principal,
        date_from=start,
        date_to=end,
        account=account,
        scope=scope,
        tag=tag,
        accounts=accounts,
    )
    history = []
    for offset in range(1, BASELINE_MONTHS + 1):
        prior_start = add_months(start, -offset)
        history.append(
            spending_by_category_report(
                principal,
                date_from=prior_start,
                date_to=month_end(prior_start),
                account=account,
                scope=scope,
                tag=tag,
                accounts=accounts,
            )
        )
    flags = []
    for row in current_report.rows:
        baseline = _median_minor(_row_spending(report, row.key) for report in history)
        if not exceeds_category_baseline(row.spending_minor, baseline, percent, floor_minor):
            continue
        flags.append(
            {
                "kind": KIND_CATEGORY,
                "name": row.name,
                "key": row.key,
                "month_minor": row.spending_minor,
                "baseline_minor": _whole_minor(baseline),
                "baseline_display": format_minor(baseline),
                "month_display": row.spending_display,
                "url": spending_category_detail_url(
                    row.key, start, end, account=account, scope=scope, tag=tag
                ),
                "item_id": f"category:{row.key}",
            }
        )
    flags.sort(key=lambda item: (-item["month_minor"], item["name"]))
    return flags


class _RunningMedian:
    """Exact integer halves: lower is a max heap, upper a min heap."""

    def __init__(self):
        self.lower = []
        self.upper = []

    def __len__(self):
        return len(self.lower) + len(self.upper)

    def add(self, amount):
        if not self.lower or amount <= -self.lower[0]:
            heappush(self.lower, -amount)
        else:
            heappush(self.upper, amount)
        if len(self.lower) > len(self.upper) + 1:
            heappush(self.upper, -heappop(self.lower))
        elif len(self.upper) > len(self.lower):
            heappush(self.lower, -heappop(self.upper))

    def median(self):
        if not self.lower:
            return Decimal("0")
        if len(self.lower) == len(self.upper):
            return (Decimal(-self.lower[0]) + Decimal(self.upper[0])) / Decimal("2")
        return Decimal(-self.lower[0])


def _charges(principal, accounts, *, date_to, excluded):
    return (
        Transaction.objects.visible_to(principal)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            amount_minor__lt=0,
            transaction_date__lte=date_to,
            account__in=accounts,
        )
        .exclude(pk__in=excluded)
        .order_by("transaction_date", "pk")
        .values_list(
            "pk", "transaction_date", "description", "amount_minor", "currency", "account_id", named=True
        )
        .iterator(chunk_size=2000)
    )


def _merchant_flag(txn, key, prior, threshold):
    amount = abs(txn.amount_minor)
    if not prior:
        if threshold is None or threshold <= 0 or amount < threshold:
            return None
        return {
            "kind": KIND_NEW_MERCHANT,
            "name": txn.description,
            "merchant_key": key,
            "amount_minor": amount,
            "amount_display": format_minor(amount, txn.currency),
            "date": txn.transaction_date.isoformat(),
            "url": reverse("transaction-edit", args=[txn.pk]),
            "transaction_id": txn.pk,
            "account_id": txn.account_id,
            "item_id": f"new_merchant:{key}",
        }
    baseline = prior.median()
    if not exceeds_merchant_median(amount, baseline, prior_count=len(prior)):
        return None
    return {
        "kind": KIND_MERCHANT,
        "name": txn.description,
        "merchant_key": key,
        "amount_minor": amount,
        "median_minor": _whole_minor(baseline),
        "median_display": format_minor(baseline, txn.currency),
        "amount_display": format_minor(amount, txn.currency),
        "date": txn.transaction_date.isoformat(),
        "url": reverse("transaction-edit", args=[txn.pk]),
        "transaction_id": txn.pk,
        "account_id": txn.account_id,
        "item_id": f"merchant:{txn.pk}",
    }


def _merchant_flags(principal, start, end, prefs, accounts, excluded):
    by_key = {}
    flags = []
    # Ordered by date and ID, so same-day lower IDs are strictly earlier.
    # Evaluate before insertion: each eligible charge enters its history once.
    for txn in _charges(principal, accounts, date_to=end, excluded=excluded):
        key = merchant_key(txn.description)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = _RunningMedian()
        prior = by_key[key]
        if start <= txn.transaction_date <= end:
            flag = _merchant_flag(txn, key, prior, prefs.large_transaction_minor)
            if flag is not None:
                flags.append(flag)
        prior.add(abs(txn.amount_minor))
    flags.sort(key=lambda item: (-item["amount_minor"], item["name"], item.get("transaction_id") or 0))
    return flags


def compute_unusual_flags(
    principal,
    month,
    *,
    account=None,
    scope="",
    tag=None,
    accounts=None,
):
    """Flags for one month using the viewer's visible cash-flow accounts (and optional filters)."""
    person = _person_for(principal)
    start = month_start(month)
    end = month_end(start)
    prefs = settings_for(person)
    chosen = selected_accounts(
        principal,
        account=account,
        scope=scope,
        cash_flow_only=True,
        accounts=accounts,
    )
    excluded = _excluded_transfer_ids(principal)
    category_flags = _category_flags(
        principal,
        start,
        end,
        prefs,
        account=account,
        scope=scope,
        tag=tag,
        accounts=chosen,
    )
    merchant_flags = _merchant_flags(principal, start, end, prefs, chosen, excluded)
    return category_flags + merchant_flags


def unusual_review_url(month):
    return f"{reverse('monthly-review')}?month={month_start(month).isoformat()[:7]}"


MAX_DEDUPE_ITEM = 150


def _dedupe_key(stamp, item_id):
    # Alert.dedupe_key holds 200 characters; only an item id too long for it is hashed,
    # so ordinary keys stay readable and stable.
    item = str(item_id)
    if len(item) > MAX_DEDUPE_ITEM:
        item = "h:" + sha256(item.encode()).hexdigest()
    return f"unusual:{stamp}:{item}"


def raise_unusual_alerts(person, month, flags):
    month = month_start(month)
    stamp = month.isoformat()[:7]
    created = []
    accounts = {row.pk: row for row in Account.objects.filter(pk__in=[flag.get("account_id") for flag in flags if flag.get("account_id")])}
    for flag in flags:
        kind = flag["kind"]
        if kind == KIND_CATEGORY:
            title = f"Unusual spending in {flag['name']} this month"
        elif kind == KIND_NEW_MERCHANT:
            title = f"New merchant {flag['name']} above your large-transaction threshold"
        else:
            title = f"Unusual charge at {flag['name']}"
        created.extend(
            raise_alert(
                [person],
                Alert.Kind.UNUSUAL_SPENDING,
                title,
                flag.get("url") or unusual_review_url(month),
                _dedupe_key(stamp, flag["item_id"]),
                account=accounts.get(flag.get("account_id")),
            )
        )
    return created
