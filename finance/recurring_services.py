from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
import re

from django.core.exceptions import PermissionDenied
from django.db import transaction

from .category_services import exclusion_exists_for
from .lifecycle_services import lock_actor_household
from .models import Person, RecurringSeries, RecurringSeriesMember, Transaction


_DENIED = "Operation is not permitted."
MAX_AMOUNT_VARIANCE = Decimal("0.25")
CADENCE_DAYS = {
    RecurringSeries.Cadence.WEEKLY: (7, 3),
    RecurringSeries.Cadence.BIWEEKLY: (14, 3),
    RecurringSeries.Cadence.MONTHLY: (30, 3),
    RecurringSeries.Cadence.QUARTERLY: (91, 4),
    RecurringSeries.Cadence.ANNUAL: (365, 5),
}
CADENCE_ORDER = (
    RecurringSeries.Cadence.WEEKLY,
    RecurringSeries.Cadence.BIWEEKLY,
    RecurringSeries.Cadence.MONTHLY,
    RecurringSeries.Cadence.QUARTERLY,
    RecurringSeries.Cadence.ANNUAL,
)


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            pass
    raise PermissionDenied(_DENIED)


def merchant_key(description):
    tokens = re.sub(r"[^a-z]+", " ", description.casefold()).split()
    # Cut here, not at save time, so grouping and the stored key always agree.
    return " ".join(tokens)[: RecurringSeries._meta.get_field("merchant_key").max_length].rstrip()


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def add_cadence(value: date, cadence: str) -> date:
    if cadence == RecurringSeries.Cadence.WEEKLY:
        return value + timedelta(days=7)
    if cadence == RecurringSeries.Cadence.BIWEEKLY:
        return value + timedelta(days=14)
    if cadence == RecurringSeries.Cadence.MONTHLY:
        return _add_months(value, 1)
    if cadence == RecurringSeries.Cadence.QUARTERLY:
        return _add_months(value, 3)
    if cadence == RecurringSeries.Cadence.ANNUAL:
        try:
            return value.replace(year=value.year + 1)
        except ValueError:
            return value.replace(year=value.year + 1, day=28)
    raise ValueError("Unknown cadence")


def _within_tolerance(actual: date, expected: date, cadence: str) -> bool:
    _interval, tolerance = CADENCE_DAYS[cadence]
    return abs((actual - expected).days) <= tolerance


def _median_minor(values):
    ordered = sorted(abs(value) for value in values)
    return ordered[len(ordered) // 2]


def amounts_within_tolerance(minors):
    values = list(minors)
    if not values:
        return False
    median = _median_minor(values)
    if median == 0:
        return False
    limit = Decimal(median) * MAX_AMOUNT_VARIANCE
    return all(abs(Decimal(abs(value) - median)) <= limit for value in values)


def _in_confirmed_amount_band(confirmed_typical, detected_typical):
    center = abs(confirmed_typical)
    if center == 0:
        return False
    limit = Decimal(center) * MAX_AMOUNT_VARIANCE
    return abs(Decimal(abs(detected_typical) - center)) <= limit


def _collapse_same_day(transactions):
    by_key = {}
    for item in sorted(transactions, key=lambda row: (row.transaction_date, abs(row.amount_minor), row.pk)):
        by_key.setdefault((item.transaction_date, abs(item.amount_minor)), item)
    return list(by_key.values())


def _longest_chain(transactions, cadence):
    ordered = sorted(transactions, key=lambda row: (row.transaction_date, row.pk))
    length = [1] * len(ordered)
    previous = [-1] * len(ordered)
    for start, source in enumerate(ordered):
        expected = add_cadence(source.transaction_date, cadence)
        for end in range(start + 1, len(ordered)):
            actual = ordered[end].transaction_date
            if actual < expected - timedelta(days=CADENCE_DAYS[cadence][1]):
                continue
            if not _within_tolerance(actual, expected, cadence):
                continue
            candidate = length[start] + 1
            if candidate > length[end]:
                length[end] = candidate
                previous[end] = start
    if not length:
        return []
    end_index = max(range(len(length)), key=lambda index: (length[index], -index))
    if length[end_index] < 2:
        return []
    chain = []
    index = end_index
    while index >= 0:
        chain.append(ordered[index])
        index = previous[index]
    chain.reverse()
    return chain


def _amount_spread_ratio(minors):
    median = Decimal(_median_minor(minors))
    widest = max(abs(Decimal(abs(value)) - median) for value in minors)
    if median == 0:
        return Decimal("1")
    return widest / median


def _confidence_and_reasons(chain, cadence):
    minors = [item.amount_minor for item in chain]
    exact = len({abs(value) for value in minors}) == 1
    spread = _amount_spread_ratio(minors)
    date_slop = []
    for left, right in zip(chain, chain[1:]):
        expected = add_cadence(left.transaction_date, cadence)
        date_slop.append(abs((right.transaction_date - expected).days))
    max_slop = max(date_slop) if date_slop else 0
    count = len(chain)
    reasons = [
        f"{count} occurrences at a {cadence} interval",
        "amounts match exactly" if exact else f"amounts vary by {int(spread * 100)}% (within 25%)",
        f"dates within {max_slop} day(s) of expected",
    ]
    if count < 3:
        return RecurringSeries.Confidence.LOW, tuple(reasons), RecurringSeries.Status.POSSIBLE
    if exact and max_slop <= 1:
        confidence = RecurringSeries.Confidence.HIGH
    elif exact or max_slop <= 2:
        confidence = RecurringSeries.Confidence.MEDIUM
    else:
        confidence = RecurringSeries.Confidence.LOW
    return confidence, tuple(reasons), RecurringSeries.Status.SUGGESTED


def _display_name(chain):
    counts = Counter(item.description.strip() or merchant_key(item.description) for item in chain)
    # Descriptions are unbounded; the stored name is not.
    return counts.most_common(1)[0][0][: RecurringSeries._meta.get_field("display_name").max_length]


def _fingerprint(transaction_ids):
    payload = ",".join(str(pk) for pk in sorted(transaction_ids))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DetectedSeries:
    merchant_key: str
    display_name: str
    cadence: str
    typical_amount_minor: int
    currency: str
    confidence: str
    reasons: tuple
    status: str
    transaction_ids: tuple
    fingerprint: str


def _cluster_by_amount(transactions):
    clusters = []
    for item in sorted(transactions, key=lambda row: (abs(row.amount_minor), row.pk)):
        placed = False
        for cluster in clusters:
            trial_amounts = [row.amount_minor for row in cluster] + [item.amount_minor]
            if amounts_within_tolerance(trial_amounts):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    return clusters


def _detected_series(key, cadence, chain):
    confidence, reasons, status = _confidence_and_reasons(chain, cadence)
    typical = -_median_minor(item.amount_minor for item in chain)
    ids = tuple(item.pk for item in chain)
    return DetectedSeries(
        merchant_key=key,
        display_name=_display_name(chain),
        cadence=cadence,
        typical_amount_minor=typical,
        currency=chain[0].currency,
        confidence=confidence,
        reasons=reasons,
        status=status,
        transaction_ids=ids,
        fingerprint=_fingerprint(ids),
    )


def _accept_chain(key, cadence, chain, detected, used):
    detected.append(_detected_series(key, cadence, chain))
    used.update(item.pk for item in chain)


def _pick_cadence_chain(cluster):
    collapsed = _collapse_same_day(cluster)
    best = []
    best_cadence = None
    for cadence in CADENCE_ORDER:
        chain = _longest_chain(collapsed, cadence)
        if len(chain) < 2:
            continue
        if len(chain) > len(best):
            best = chain
            best_cadence = cadence
            continue
        if len(chain) == len(best) and best_cadence is not None:
            current_interval, _ = CADENCE_DAYS[cadence]
            best_interval, _ = CADENCE_DAYS[best_cadence]
            if current_interval > best_interval:
                best = chain
                best_cadence = cadence
    if best_cadence is None:
        return None, []
    return best_cadence, best


def _used_transaction_ids(series_list):
    used = set()
    for series in series_list:
        used.update(series.transaction_ids)
    return used


def detect_recurring_series(transactions):
    grouped = defaultdict(list)
    for item in transactions:
        key = merchant_key(item.description)
        if key:
            grouped[key].append(item)
    detected = []
    for key, group in grouped.items():
        used = set()
        while True:
            candidates = [item for item in group if item.pk not in used]
            if len(candidates) < 2:
                break
            cadence, chain = _pick_cadence_chain(candidates)
            if cadence is None:
                break
            if amounts_within_tolerance(item.amount_minor for item in chain):
                _accept_chain(key, cadence, chain, detected, used)
                continue
            split_any = False
            for cluster in _cluster_by_amount(chain):
                sub_cadence, sub_chain = _pick_cadence_chain(cluster)
                if sub_cadence is None or len(sub_chain) < 3:
                    continue
                if not amounts_within_tolerance(item.amount_minor for item in sub_chain):
                    continue
                _accept_chain(key, sub_cadence, sub_chain, detected, used)
                split_any = True
            if not split_any:
                used.update(item.pk for item in chain)
    detected.sort(key=lambda item: (-len(item.transaction_ids), item.merchant_key, item.cadence))
    chosen = []
    for item in detected:
        if set(item.transaction_ids) & _used_transaction_ids(chosen):
            continue
        chosen.append(item)
    return chosen


def candidate_transactions(principal):
    person = _person_for(principal)
    return list(
        Transaction.objects.visible_to(person)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            amount_minor__lt=0,
        )
        .annotate(_excluded=exclusion_exists_for(person))
        .filter(_excluded=False)
        .select_related("account")
        .order_by("transaction_date", "pk")
    )


def _apply_detection(series, detected):
    series.merchant_key = detected.merchant_key
    series.display_name = detected.display_name
    series.cadence = detected.cadence
    series.typical_amount_minor = detected.typical_amount_minor
    series.currency = detected.currency
    series.confidence = detected.confidence
    series.reasons = list(detected.reasons)
    series.fingerprint = detected.fingerprint
    series.is_active = True
    if series.status not in (RecurringSeries.Status.CONFIRMED, RecurringSeries.Status.DISMISSED):
        series.status = detected.status
    series.save()
    RecurringSeriesMember.objects.filter(series=series).delete()
    RecurringSeriesMember.objects.bulk_create(
        RecurringSeriesMember(series=series, transaction_id=pk) for pk in detected.transaction_ids
    )


def _fingerprint_taken(person, fingerprint, *, exclude_pk):
    return RecurringSeries.objects.filter(person=person, fingerprint=fingerprint).exclude(pk=exclude_pk).exists()


def _match_confirmed(confirmed, detected, kept_ids):
    for series in confirmed:
        if series.pk in kept_ids:
            continue
        if series.merchant_key != detected.merchant_key or series.cadence != detected.cadence:
            continue
        if _in_confirmed_amount_band(series.typical_amount_minor, detected.typical_amount_minor):
            return series
    return None


def _find_open_match(open_rows, open_by_fingerprint, kept_ids, item):
    match = open_by_fingerprint.get(item.fingerprint)
    # A row already refreshed this pass belongs to another chain now.
    if match is not None and match.pk not in kept_ids:
        return match
    for series in open_rows:
        if series.pk in kept_ids:
            continue
        if series.merchant_key == item.merchant_key and series.cadence == item.cadence:
            return series
    return None


def _create_series(person, detected):
    created = RecurringSeries.objects.create(
        person=person,
        merchant_key=detected.merchant_key,
        display_name=detected.display_name,
        cadence=detected.cadence,
        typical_amount_minor=detected.typical_amount_minor,
        currency=detected.currency,
        status=detected.status,
        confidence=detected.confidence,
        reasons=list(detected.reasons),
        fingerprint=detected.fingerprint,
    )
    RecurringSeriesMember.objects.bulk_create(
        RecurringSeriesMember(series=created, transaction_id=pk) for pk in detected.transaction_ids
    )
    return created


def _confirmed_owner(confirmed, item, kept_ids):
    """The confirmed series sharing the most transactions with a detected chain.

    Shared transactions mean the same subscription, even after corrected
    amounts or new occurrences change its fingerprint and amount band, so it
    must never become a second, overlapping series.
    """
    detected_ids = set(item.transaction_ids)
    best, best_overlap = None, 0
    for series in confirmed:
        if series.pk in kept_ids:
            continue
        overlap = len(detected_ids & {member.transaction_id for member in series.members.all()})
        if overlap > best_overlap:
            best, best_overlap = series, overlap
    return best


def _confirmed_target(person, item, confirmed, kept_ids):
    target = _confirmed_owner(confirmed, item, kept_ids) or _match_confirmed(confirmed, item, kept_ids)
    if target is not None and _fingerprint_taken(person, item.fingerprint, exclude_pk=target.pk):
        return None
    return target


def _upsert_detected(person, item, *, dismissed_fingerprints, confirmed, open_rows, open_by_fingerprint, kept_ids):
    if item.fingerprint in dismissed_fingerprints:
        return
    target = _confirmed_target(person, item, confirmed, kept_ids)
    if target is None:
        target = _find_open_match(open_rows, open_by_fingerprint, kept_ids, item)
    if target is not None:
        if not _fingerprint_taken(person, item.fingerprint, exclude_pk=target.pk):
            _apply_detection(target, item)
            kept_ids.add(target.pk)
        return
    # Another row (for example a confirmed series already refreshed this
    # pass) owns the fingerprint: never create a duplicate.
    if not _fingerprint_taken(person, item.fingerprint, exclude_pk=None):
        kept_ids.add(_create_series(person, item).pk)


def _drop_stale_open_rows(open_rows, kept_ids):
    stale = [series for series in open_rows if series.pk not in kept_ids]
    RecurringSeriesMember.objects.filter(series__in=stale).delete()
    RecurringSeries.objects.filter(pk__in=[series.pk for series in stale]).delete()


def _reconcile_unmatched_confirmed(confirmed, kept_ids, eligible_ids):
    stale = [series for series in confirmed if series.pk not in kept_ids]
    # A transaction belongs to one series: anything a refreshed series now
    # holds no longer counts toward an unmatched one.
    claimed = set(
        RecurringSeriesMember.objects.filter(series_id__in=kept_ids)
        .exclude(series__status=RecurringSeries.Status.DISMISSED)
        .values_list("transaction_id", flat=True)
    )
    for series in stale:
        remaining = [
            member.transaction_id
            for member in series.members.all()
            if member.transaction_id in eligible_ids and member.transaction_id not in claimed
        ]
        if remaining:
            RecurringSeriesMember.objects.filter(series=series).exclude(transaction_id__in=remaining).delete()
            # The stored name may come from a transaction the person can no
            # longer see, so rename from what remains.
            series.display_name = _display_name(Transaction.objects.filter(pk__in=remaining).order_by("pk"))
            series.save(update_fields=("display_name", "updated_at"))
            continue
        RecurringSeriesMember.objects.filter(series=series).delete()
        RecurringSeries.objects.filter(pk=series.pk).update(is_active=False)


@transaction.atomic
def refresh_recurring_series(principal):
    person = _person_for(principal)
    lock_actor_household(person)
    candidates = candidate_transactions(person)
    detected = detect_recurring_series(candidates)
    existing = list(RecurringSeries.objects.select_for_update(of=("self",)).filter(person=person).order_by("pk"))
    dismissed_fingerprints = {
        series.fingerprint for series in existing if series.status == RecurringSeries.Status.DISMISSED
    }
    confirmed = [series for series in existing if series.status == RecurringSeries.Status.CONFIRMED]
    open_rows = [
        series
        for series in existing
        if series.status in (RecurringSeries.Status.POSSIBLE, RecurringSeries.Status.SUGGESTED)
    ]
    kept_ids = {series.pk for series in existing if series.status == RecurringSeries.Status.DISMISSED}
    open_by_fingerprint = {series.fingerprint: series for series in open_rows}
    for item in detected:
        _upsert_detected(
            person,
            item,
            dismissed_fingerprints=dismissed_fingerprints,
            confirmed=confirmed,
            open_rows=open_rows,
            open_by_fingerprint=open_by_fingerprint,
            kept_ids=kept_ids,
        )
    _drop_stale_open_rows(open_rows, kept_ids)
    _reconcile_unmatched_confirmed(confirmed, kept_ids, {row.pk for row in candidates})
    return RecurringSeries.objects.visible_to(person)


def _visible_series(principal, series_id):
    person = _person_for(principal)
    series = RecurringSeries.objects.visible_to(person).filter(pk=series_id).first()
    if series is None:
        raise PermissionDenied(_DENIED)
    return person, series


_OPEN_STATUSES = (RecurringSeries.Status.POSSIBLE, RecurringSeries.Status.SUGGESTED)


def _set_open_series_status(principal, series_id, status):
    # Take the household lock first, then check visibility and status on the
    # locked row: a concurrent refresh may delete or change the suggestion
    # while this request waits, and that must be a denial, not an error.
    person = _person_for(principal)
    lock_actor_household(person)
    _visible_series(person, series_id)
    series = RecurringSeries.objects.select_for_update(of=("self",)).filter(pk=series_id).first()
    if series is None or series.status not in _OPEN_STATUSES:
        raise PermissionDenied(_DENIED)
    series.status = status
    series.save(update_fields=("status", "updated_at"))
    return series


@transaction.atomic
def confirm_recurring_series(principal, series_id):
    return _set_open_series_status(principal, series_id, RecurringSeries.Status.CONFIRMED)


@transaction.atomic
def dismiss_recurring_series(principal, series_id):
    return _set_open_series_status(principal, series_id, RecurringSeries.Status.DISMISSED)


def confirmed_totals(series_queryset):
    monthly = 0
    annual = 0
    for series in series_queryset:
        if series.status != RecurringSeries.Status.CONFIRMED or not series.is_active:
            continue
        monthly += series.monthly_minor
        annual += series.annual_minor
    return monthly, annual
