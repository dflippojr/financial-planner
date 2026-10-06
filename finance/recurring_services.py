from bisect import bisect_left, bisect_right
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from decimal import Decimal
from hashlib import sha256
import re

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .category_services import exclusion_exists_for
from .lifecycle_services import lock_actor_household
from .models import Person, RecurringExclusion, RecurringSeries, RecurringSeriesMember, Transaction


_DENIED = "Operation is not permitted."
MANUAL_REASON = "grouping edited manually"
MAX_AMOUNT_VARIANCE = Decimal("0.25")
# Outlier removal makes the search superlinear in a merchant's charges, so a
# merchant with more charges than this is left out of detection (and listed on
# the page) rather than holding a request for minutes.
MAX_CHARGES_PER_MERCHANT = 600
# Bump when detection rules change so every household detects again.
DETECTION_VERSION = "1"
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


@lru_cache(maxsize=65536)
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
    """True when detected_typical is within 25% of confirmed_typical (the level).

    The limit is always 25% of that level, not 25% of the larger of the two
    amounts. Pairwise clustering via amounts_within_tolerance still uses the
    cluster median, which for two values is the larger one.
    """
    center = abs(confirmed_typical)
    if center == 0:
        return False
    limit = Decimal(center) * MAX_AMOUNT_VARIANCE
    return abs(Decimal(abs(detected_typical) - center)) <= limit


def typical_amount_minor_from(transactions):
    ordered = sorted(transactions, key=lambda row: (row.transaction_date, row.pk))
    window = ordered[-3:] if len(ordered) > 3 else ordered
    return -_median_minor(item.amount_minor for item in window)


def _step_ratio(left_minor, right_minor):
    center = Decimal(abs(left_minor))
    if center == 0:
        return Decimal("1")
    return abs(Decimal(abs(right_minor)) - center) / center


def _largest_consecutive_step(chain):
    if len(chain) < 2:
        return Decimal("0")
    return max(_step_ratio(left.amount_minor, right.amount_minor) for left, right in zip(chain, chain[1:]))


def _current_level(accepted):
    return _median_minor(item.amount_minor for item in accepted[-2:])


def _evaluate_rolling_chain(chain):
    ordered = sorted(chain, key=lambda row: (row.transaction_date, row.pk))
    accepted = []
    breakers = []
    for index, item in enumerate(ordered):
        if not accepted:
            accepted.append(item)
            continue
        level = _current_level(accepted)
        if _in_confirmed_amount_band(level, item.amount_minor):
            accepted.append(item)
            continue
        nxt = ordered[index + 1] if index + 1 < len(ordered) else None
        if (
            len(accepted) >= 2
            and nxt is not None
            and _in_confirmed_amount_band(item.amount_minor, nxt.amount_minor)
            and not _in_confirmed_amount_band(level, nxt.amount_minor)
        ):
            accepted = [item]
            continue
        center = Decimal(abs(level)) or Decimal(1)
        score = abs(Decimal(abs(item.amount_minor)) - Decimal(abs(level))) / center
        breakers.append((score, item.pk, item))
    return not breakers, breakers


def _chain_passes_rolling_band(chain):
    passed, _breakers = _evaluate_rolling_chain(chain)
    return passed


def _worst_rolling_band_breaker(chain):
    _passed, breakers = _evaluate_rolling_chain(chain)
    farthest = _farthest_from_chain_median(chain)
    remaining = [item for item in chain if item.pk != farthest.pk]
    if len(remaining) >= 2 and _chain_passes_rolling_band(remaining):
        return farthest
    if breakers:
        return max(breakers)[2]
    return farthest


def _same_day_key(item):
    return (item.transaction_date, abs(item.amount_minor))


def _collapse_same_day(transactions):
    by_key = {}
    for item in sorted(transactions, key=lambda row: (row.transaction_date, abs(row.amount_minor), row.pk)):
        by_key.setdefault((item.transaction_date, abs(item.amount_minor)), item)
    return sorted(by_key.values(), key=lambda row: (row.transaction_date, row.pk))


def _longest_chain(ordered, dates, cadence):
    """Longest cadence chain. `ordered` is sorted by (date, pk); `dates` is its dates."""
    tolerance = CADENCE_DAYS[cadence][1]
    days = [value.toordinal() for value in dates]
    length = [1] * len(ordered)
    previous = [-1] * len(ordered)
    for start, source in enumerate(dates):
        # A step can only land within the cadence tolerance, so look at that
        # window of the sorted dates instead of every later charge.
        expected = add_cadence(source, cadence).toordinal()
        low = bisect_left(days, expected - tolerance, start + 1)
        high = bisect_right(days, expected + tolerance, low)
        candidate = length[start] + 1
        for end in range(low, high):
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


def _confidence_and_reasons(chain, cadence):
    minors = [item.amount_minor for item in chain]
    exact = len({abs(value) for value in minors}) == 1
    step = _largest_consecutive_step(chain)
    date_slop = []
    for left, right in zip(chain, chain[1:]):
        expected = add_cadence(left.transaction_date, cadence)
        date_slop.append(abs((right.transaction_date - expected).days))
    max_slop = max(date_slop) if date_slop else 0
    count = len(chain)
    if exact:
        amount_reason = "amounts match exactly"
    else:
        amount_reason = f"amounts change by up to {int(step * 100)}% between charges (within 25%)"
    reasons = [
        f"{count} occurrences at a {cadence} interval",
        amount_reason,
        f"dates within {max_slop} day(s) of expected",
    ]
    first_abs = abs(chain[0].amount_minor)
    last_abs = abs(chain[-1].amount_minor)
    if first_abs and _step_ratio(chain[0].amount_minor, chain[-1].amount_minor) > MAX_AMOUNT_VARIANCE:
        direction = "rose" if last_abs >= first_abs else "fell"
        overall = int(_step_ratio(chain[0].amount_minor, chain[-1].amount_minor) * 100)
        reasons.append(f"price {direction} {overall}% overall")
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
    sizes = []
    for item in sorted(transactions, key=lambda row: (abs(row.amount_minor), row.pk)):
        value = abs(item.amount_minor)
        for cluster, size in zip(clusters, sizes):
            # Same test as amounts_within_tolerance on cluster + item. Items
            # arrive in ascending amount, so only the first and the new value
            # can sit furthest from the median.
            count = len(cluster) + 1
            median = cluster[count // 2] if count // 2 < len(cluster) else value
            if median and (median - cluster[0]) * 4 <= median and (value - median) * 4 <= median:
                cluster.append(value)
                size.append(item)
                break
        else:
            clusters.append([value])
            sizes.append([item])
    return sizes


def _detected_series(key, cadence, chain):
    ordered = sorted(chain, key=lambda row: (row.transaction_date, row.pk))
    confidence, reasons, status = _confidence_and_reasons(ordered, cadence)
    typical = typical_amount_minor_from(ordered)
    ids = tuple(item.pk for item in ordered)
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


class _CadenceChains:
    """Longest chain per cadence for a pool of charges, kept across removals.

    Dropping charges that are not on a cadence's chain cannot change that
    chain, so only the cadences whose chain lost a charge are searched again.
    """

    def __init__(self, transactions):
        self._by_pk = {item.pk: item for item in transactions}
        self._key_counts = Counter(_same_day_key(item) for item in transactions)
        self._set_collapsed(_collapse_same_day(transactions))
        self._chains = {}

    def _set_collapsed(self, collapsed):
        self._collapsed = collapsed
        self._dates = [item.transaction_date for item in collapsed]

    def remove(self, removed_pks):
        removed = [self._by_pk.pop(pk) for pk in removed_pks]
        shared = False
        for item in removed:
            key = _same_day_key(item)
            self._key_counts[key] -= 1
            shared = shared or self._key_counts[key] > 0
        if shared:
            # A duplicate steps in for a removed row: nothing is reusable.
            self._chains = {}
            self._set_collapsed(_collapse_same_day(self._by_pk.values()))
            return
        self._chains = {
            cadence: chain
            for cadence, chain in self._chains.items()
            if not any(item.pk in removed_pks for item in chain)
        }
        self._set_collapsed([item for item in self._collapsed if item.pk not in removed_pks])

    def pick(self):
        best = []
        best_cadence = None
        for cadence in CADENCE_ORDER:
            if cadence not in self._chains:
                self._chains[cadence] = _longest_chain(self._collapsed, self._dates, cadence)
            chain = self._chains[cadence]
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


def _pick_cadence_chain(cluster):
    return _CadenceChains(cluster).pick()


def _farthest_from_chain_median(chain):
    median = Decimal(_median_minor(item.amount_minor for item in chain))
    return max(chain, key=lambda item: (abs(Decimal(abs(item.amount_minor)) - median), -item.pk))


def _pick_tolerant_cadence_chain(candidates):
    pool_size = len(candidates)
    chains = _CadenceChains(candidates)
    while pool_size >= 2:
        cadence, chain = chains.pick()
        if cadence is None:
            return None, []
        if _chain_passes_rolling_band(chain):
            return cadence, chain
        outlier = _worst_rolling_band_breaker(chain)
        chains.remove({outlier.pk})
        pool_size -= 1
    return None, []


def _used_transaction_ids(series_list):
    used = set()
    for series in series_list:
        used.update(series.transaction_ids)
    return used


def _group_by_merchant(transactions):
    grouped = defaultdict(list)
    for item in transactions:
        key = merchant_key(item.description)
        if key:
            grouped[key].append(item)
    return grouped


def _split_chain_by_amount(key, chain, detected, used):
    """Accept each amount cluster of a mixed chain that is itself a valid series."""
    split_any = False
    for cluster in _cluster_by_amount(chain):
        sub_cadence, sub_chain = _pick_cadence_chain(cluster)
        # Two occurrences still make a "possible" series (#17 decisions).
        if sub_cadence is None or len(sub_chain) < 2:
            continue
        if not amounts_within_tolerance(item.amount_minor for item in sub_chain):
            continue
        _accept_chain(key, sub_cadence, sub_chain, detected, used)
        split_any = True
    return split_any


def _detect_for_merchant(key, group, detected):
    used = set()
    while True:
        candidates = [item for item in group if item.pk not in used]
        if len(candidates) < 2:
            return
        cadence, chain = _pick_tolerant_cadence_chain(candidates)
        if cadence is not None:
            _accept_chain(key, cadence, chain, detected, used)
            continue
        # Outlier exclusion could not form a tolerant chain; split remaining
        # charges by amount the way mixed-chain fallback already did.
        cadence, chain = _pick_cadence_chain(candidates)
        if cadence is None:
            return
        if not _split_chain_by_amount(key, candidates, detected, used):
            used.update(item.pk for item in chain)


def _drop_overlaps(detected):
    detected.sort(key=lambda item: (-len(item.transaction_ids), item.merchant_key, item.cadence))
    chosen = []
    for item in detected:
        if set(item.transaction_ids) & _used_transaction_ids(chosen):
            continue
        chosen.append(item)
    return chosen


def detect_recurring_series_with_skips(transactions):
    """Detected series plus the merchant keys left out for having too many charges."""
    detected = []
    skipped = []
    for key, group in _group_by_merchant(transactions).items():
        if len(group) > MAX_CHARGES_PER_MERCHANT:
            skipped.append(key)
            continue
        _detect_for_merchant(key, group, detected)
    return _drop_overlaps(detected), sorted(skipped)


def detect_recurring_series(transactions):
    return detect_recurring_series_with_skips(transactions)[0]


def _candidate_queryset(person):
    excluded_ids = RecurringExclusion.objects.filter(person=person).values("transaction_id")
    return (
        Transaction.objects.visible_to(person)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            amount_minor__lt=0,
        )
        .exclude(pk__in=excluded_ids)
        .annotate(_excluded=exclusion_exists_for(person))
        .filter(_excluded=False)
    )


def candidate_transactions(principal):
    person = _person_for(principal)
    return list(_candidate_queryset(person).select_related("account").order_by("transaction_date", "pk"))


def _inputs_signature(person):
    """Fingerprint of everything detection reads from a person's charges."""
    digest = sha256(DETECTION_VERSION.encode("utf-8"))
    rows = (
        _candidate_queryset(person)
        .order_by("pk")
        .values_list("pk", "transaction_date", "amount_minor", "currency", "description")
    )
    for row in rows.iterator(chunk_size=5000):
        digest.update(repr(row).encode("utf-8"))
    return digest.hexdigest()


def grouping_candidate_transactions(principal):
    """Eligible charges for add, including rows excluded only from detection."""
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


def _active_member_transaction_ids(person, *, exclude_series_id=None):
    query = RecurringSeriesMember.objects.filter(series__person=person, series__is_active=True).exclude(
        series__status=RecurringSeries.Status.DISMISSED
    )
    if exclude_series_id is not None:
        query = query.exclude(series_id=exclude_series_id)
    return set(query.values_list("transaction_id", flat=True))


def _assign_fingerprint(series, member_ids):
    fingerprint = _fingerprint(member_ids)
    if _fingerprint_taken(series.person, fingerprint, exclude_pk=series.pk):
        return
    series.fingerprint = fingerprint


def _member_transactions(series):
    return [
        member.transaction
        for member in series.members.select_related("transaction").order_by(
            "transaction__transaction_date", "transaction_id"
        )
    ]


def _recompute_series_from_members(series, *, extra_reasons=()):
    transactions = _member_transactions(series)
    if not transactions:
        series.is_active = False
        series.save(update_fields=("is_active", "updated_at"))
        return series
    ordered = sorted(transactions, key=lambda row: (row.transaction_date, row.pk))
    confidence, reasons, status = _confidence_and_reasons(ordered, series.cadence)
    reason_list = list(reasons)
    for reason in extra_reasons:
        if reason not in reason_list:
            reason_list.append(reason)
    if (
        series.members.filter(source=RecurringSeriesMember.Source.MANUAL).exists()
        and MANUAL_REASON not in reason_list
    ):
        reason_list.append(MANUAL_REASON)
    series.typical_amount_minor = typical_amount_minor_from(ordered)
    series.confidence = confidence
    series.reasons = reason_list
    series.currency = ordered[0].currency
    series.is_active = True
    if series.status not in (RecurringSeries.Status.CONFIRMED, RecurringSeries.Status.DISMISSED):
        series.status = status
    _assign_fingerprint(series, [row.pk for row in ordered])
    series.save()
    return series


def _detected_items(detected):
    if isinstance(detected, DetectedSeries):
        return (detected,)
    return tuple(detected)


def _apply_detection(series, detected, *, eligible_ids, preserve_identity=False):
    items = _detected_items(detected)
    primary = items[0]
    if not preserve_identity:
        series.merchant_key = primary.merchant_key
        series.display_name = primary.display_name
        series.cadence = primary.cadence
    series.currency = primary.currency
    chain_ids = {pk for item in items for pk in item.transaction_ids}
    RecurringSeriesMember.objects.filter(series=series).exclude(transaction_id__in=eligible_ids).delete()
    RecurringSeriesMember.objects.filter(
        series=series,
        source=RecurringSeriesMember.Source.DETECTED,
    ).exclude(transaction_id__in=chain_ids).delete()
    claimed_elsewhere = _active_member_transaction_ids(series.person, exclude_series_id=series.pk)
    existing_ids = set(series.members.values_list("transaction_id", flat=True))
    RecurringSeriesMember.objects.bulk_create(
        RecurringSeriesMember(
            series=series,
            transaction_id=pk,
            source=RecurringSeriesMember.Source.DETECTED,
        )
        for pk in chain_ids
        if pk in eligible_ids and pk not in claimed_elsewhere and pk not in existing_ids
    )
    extra = []
    if series.members.filter(source=RecurringSeriesMember.Source.MANUAL).exists():
        extra.append(MANUAL_REASON)
    _recompute_series_from_members(series, extra_reasons=extra)


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
        RecurringSeriesMember(
            series=created,
            transaction_id=pk,
            source=RecurringSeriesMember.Source.DETECTED,
        )
        for pk in detected.transaction_ids
    )
    return created


def _overlapping_owner(series_list, item):
    """The active series sharing the most transactions with a detected chain.

    Shared transactions mean the same subscription, even after corrected
    amounts or new occurrences change its fingerprint and amount band, so it
    must never become a second, overlapping series.
    """
    detected_ids = set(item.transaction_ids)
    best, best_overlap = None, 0
    for series in series_list:
        if series.status == RecurringSeries.Status.DISMISSED:
            continue
        overlap = len(detected_ids & {member.transaction_id for member in series.members.all()})
        if overlap > best_overlap:
            best, best_overlap = series, overlap
    return best


def _confirmed_target(person, item, confirmed, kept_ids):
    target = _overlapping_owner(confirmed, item) or _match_confirmed(confirmed, item, kept_ids)
    if target is not None and _fingerprint_taken(person, item.fingerprint, exclude_pk=target.pk):
        return None
    return target


def _apply_overlapping_detections(confirmed, open_rows, detected, *, dismissed_fingerprints, kept_ids, eligible_ids):
    """Attach every overlapping chain for a series before pruning detected members.

    Refresh detects $100 and $130 as two clusters. After a merge both still
    overlap the surviving series; applying either cluster with deletion would
    drop the other amount's detected members and undo the merge.
    """
    pending = defaultdict(list)
    unmatched = []
    owners = confirmed + open_rows
    for item in detected:
        if item.fingerprint in dismissed_fingerprints:
            continue
        overlap = _overlapping_owner(owners, item)
        if overlap is None:
            unmatched.append(item)
            continue
        pending[overlap.pk].append(item)
    by_pk = {series.pk: series for series in owners}
    for series_pk, items in pending.items():
        series = by_pk[series_pk]
        preserve_identity = any(
            series.merchant_key != item.merchant_key or series.cadence != item.cadence for item in items
        )
        _apply_detection(series, items, eligible_ids=eligible_ids, preserve_identity=preserve_identity)
        kept_ids.add(series.pk)
    return unmatched


def _upsert_detected(
    person,
    item,
    *,
    dismissed_fingerprints,
    confirmed,
    open_rows,
    open_by_fingerprint,
    kept_ids,
    eligible_ids,
):
    if item.fingerprint in dismissed_fingerprints:
        return
    overlap = _overlapping_owner(confirmed + open_rows, item)
    if overlap is not None:
        preserve_identity = overlap.merchant_key != item.merchant_key or overlap.cadence != item.cadence
        _apply_detection(overlap, item, eligible_ids=eligible_ids, preserve_identity=preserve_identity)
        kept_ids.add(overlap.pk)
        return
    target = _confirmed_target(person, item, confirmed, kept_ids)
    if target is None:
        target = _find_open_match(open_rows, open_by_fingerprint, kept_ids, item)
    if target is not None:
        if not _fingerprint_taken(person, item.fingerprint, exclude_pk=target.pk):
            _apply_detection(target, item, eligible_ids=eligible_ids)
            kept_ids.add(target.pk)
        return
    claimed = _active_member_transaction_ids(person)
    if claimed & set(item.transaction_ids):
        return
    # Another row (for example a confirmed series already refreshed this
    # pass) owns the fingerprint: never create a duplicate.
    if not _fingerprint_taken(person, item.fingerprint, exclude_pk=None):
        kept_ids.add(_create_series(person, item).pk)


def _drop_stale_open_rows(open_rows, kept_ids):
    stale = [series for series in open_rows if series.pk not in kept_ids]
    if not stale:
        return
    manual_ids = set(
        RecurringSeriesMember.objects.filter(
            series__in=stale,
            source=RecurringSeriesMember.Source.MANUAL,
        ).values_list("series_id", flat=True)
    )
    drop = [series for series in stale if series.pk not in manual_ids]
    for series in stale:
        if series.pk in manual_ids:
            _recompute_series_from_members(series, extra_reasons=(MANUAL_REASON,))
            kept_ids.add(series.pk)
    RecurringSeriesMember.objects.filter(series__in=drop).delete()
    RecurringSeries.objects.filter(pk__in=[series.pk for series in drop]).delete()


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


def revalidate_series_after_member_removal(principal, series_ids):
    """Revalidate series after some members were removed. Caller holds locks.

    A confirmed series with remaining eligible members stays active. A series
    left with none is deactivated. Open series with no remaining members are
    deactivated the same way.
    """
    _person_for(principal)
    ids = sorted({pk for pk in series_ids if pk is not None})
    if not ids:
        return
    locked = list(RecurringSeries.objects.select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk"))
    by_owner = {}
    for series in locked:
        by_owner.setdefault(series.person_id, []).append(series)
    for owner_id, owned in by_owner.items():
        owner = Person.objects.get(pk=owner_id)
        eligible_ids = {row.pk for row in candidate_transactions(owner)}
        RecurringSeriesMember.objects.filter(series__in=owned).exclude(transaction_id__in=eligible_ids).delete()
        confirmed = [series for series in owned if series.status == RecurringSeries.Status.CONFIRMED]
        _reconcile_unmatched_confirmed(confirmed, set(), eligible_ids)
    open_ids = [series.pk for series in locked if series.status != RecurringSeries.Status.CONFIRMED]
    if not open_ids:
        return
    still_membered = set(RecurringSeriesMember.objects.filter(series_id__in=open_ids).values_list("series_id", flat=True))
    RecurringSeries.objects.filter(pk__in=open_ids).exclude(pk__in=still_membered).update(is_active=False)


def refresh_recurring_series(principal, *, only_if_changed=False):
    """Detect recurring series and bring the stored ones in line.

    Detection runs before the household lock is taken; only the writes hold it.
    With only_if_changed, a call is cheap when none of the charges detection
    reads changed since the last run (the page view uses this).
    """
    person = _person_for(principal)
    signature = _inputs_signature(person)
    if only_if_changed and signature == Person.objects.values_list("recurring_inputs_signature", flat=True).get(
        pk=person.pk
    ):
        return _raise_review_alerts(person)
    candidates = candidate_transactions(person)
    detected, skipped = detect_recurring_series_with_skips(candidates)
    return _write_detected(person, candidates, detected, skipped, signature)


@transaction.atomic
def _write_detected(person, candidates, detected, skipped, signature):
    lock_actor_household(person)
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
    eligible_ids = {row.pk for row in candidates}
    unmatched = _apply_overlapping_detections(
        confirmed,
        open_rows,
        detected,
        dismissed_fingerprints=dismissed_fingerprints,
        kept_ids=kept_ids,
        eligible_ids=eligible_ids,
    )
    for item in unmatched:
        _upsert_detected(
            person,
            item,
            dismissed_fingerprints=dismissed_fingerprints,
            confirmed=confirmed,
            open_rows=open_rows,
            open_by_fingerprint=open_by_fingerprint,
            kept_ids=kept_ids,
            eligible_ids=eligible_ids,
        )
    _drop_stale_open_rows(open_rows, kept_ids)
    _reconcile_unmatched_confirmed(confirmed, kept_ids, eligible_ids)
    Person.objects.filter(pk=person.pk).update(
        recurring_inputs_signature=signature, recurring_skipped_merchants=skipped
    )
    return _raise_review_alerts(person)


@transaction.atomic
def _raise_review_alerts(person):
    visible = RecurringSeries.objects.visible_to(person).prefetch_related("members__transaction__account")
    from .recurring_review import raise_recurring_review_alerts

    raise_recurring_review_alerts(person, list(visible), today=timezone.localdate())
    return visible


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
    fields = ["status", "updated_at"]
    if status == RecurringSeries.Status.CONFIRMED and series.confirmed_at is None:
        series.confirmed_at = timezone.now()
        fields.append("confirmed_at")
    series.save(update_fields=fields)
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
        if series.cancelled_at is not None:
            continue
        monthly += series.monthly_minor
        annual += series.annual_minor
    return monthly, annual


def _is_grouping_series(series):
    return series.is_active and series.status != RecurringSeries.Status.DISMISSED and series.cancelled_at is None


def _lock_visible_series_rows(person, series_ids):
    unique_ids = []
    for pk in series_ids:
        if pk not in unique_ids:
            unique_ids.append(pk)
    for pk in unique_ids:
        if RecurringSeries.objects.visible_to(person).filter(pk=pk).first() is None:
            raise PermissionDenied(_DENIED)
    locked = {
        row.pk: row
        for row in RecurringSeries.objects.select_for_update(of=("self",)).filter(pk__in=unique_ids).order_by("pk")
    }
    if len(locked) != len(unique_ids):
        raise PermissionDenied(_DENIED)
    visible = set(RecurringSeries.objects.visible_to(person).filter(pk__in=unique_ids).values_list("pk", flat=True))
    if visible != set(unique_ids):
        raise PermissionDenied(_DENIED)
    rows = [locked[pk] for pk in unique_ids]
    for series in rows:
        if series.person_id != person.pk:
            raise PermissionDenied(_DENIED)
    return rows


def list_addable_transactions(principal, series, query=""):
    person = _person_for(principal)
    claimed = _active_member_transaction_ids(person)
    needle = query.casefold().strip()
    rows = []
    for txn in grouping_candidate_transactions(person):
        if txn.pk in claimed:
            continue
        if needle and needle not in txn.description.casefold():
            continue
        rows.append(txn)
    rows.sort(
        key=lambda txn: (
            merchant_key(txn.description) != series.merchant_key,
            txn.transaction_date,
            txn.pk,
        )
    )
    return rows


@transaction.atomic
def merge_recurring_series(principal, source_id, target_id):
    person = _person_for(principal)
    lock_actor_household(person)
    if source_id == target_id:
        raise PermissionDenied(_DENIED)
    source, target = _lock_visible_series_rows(person, (source_id, target_id))
    if not _is_grouping_series(source) or not _is_grouping_series(target):
        raise PermissionDenied(_DENIED)
    source_txn_ids = list(source.members.values_list("transaction_id", flat=True))
    existing_ids = set(target.members.values_list("transaction_id", flat=True))
    RecurringSeriesMember.objects.bulk_create(
        RecurringSeriesMember(
            series=target,
            transaction_id=pk,
            source=RecurringSeriesMember.Source.MANUAL,
        )
        for pk in source_txn_ids
        if pk not in existing_ids
    )
    RecurringSeriesMember.objects.filter(series=target, transaction_id__in=source_txn_ids).update(
        source=RecurringSeriesMember.Source.MANUAL
    )
    if source.status == RecurringSeries.Status.CONFIRMED or target.status == RecurringSeries.Status.CONFIRMED:
        became_confirmed = target.status != RecurringSeries.Status.CONFIRMED
        target.status = RecurringSeries.Status.CONFIRMED
        fields = ["status", "updated_at"]
        if became_confirmed and target.confirmed_at is None:
            target.confirmed_at = timezone.now()
            fields.append("confirmed_at")
        target.save(update_fields=fields)
    source.delete()
    return _recompute_series_from_members(target, extra_reasons=(MANUAL_REASON,))


@transaction.atomic
def remove_recurring_member(principal, series_id, transaction_id):
    person = _person_for(principal)
    lock_actor_household(person)
    (series,) = _lock_visible_series_rows(person, (series_id,))
    if not _is_grouping_series(series):
        raise PermissionDenied(_DENIED)
    member = (
        RecurringSeriesMember.objects.select_for_update()
        .filter(series=series, transaction_id=transaction_id)
        .first()
    )
    if member is None:
        raise PermissionDenied(_DENIED)
    if Transaction.objects.visible_to(person).filter(pk=transaction_id).first() is None:
        raise PermissionDenied(_DENIED)
    RecurringExclusion.objects.get_or_create(person=person, transaction_id=transaction_id)
    member.delete()
    if not series.members.exists():
        revalidate_series_after_member_removal(person, [series.pk])
        return RecurringSeries.objects.filter(pk=series.pk).first()
    return _recompute_series_from_members(series, extra_reasons=(MANUAL_REASON,))


@transaction.atomic
def add_recurring_members(principal, series_id, transaction_ids):
    person = _person_for(principal)
    lock_actor_household(person)
    (series,) = _lock_visible_series_rows(person, (series_id,))
    if not _is_grouping_series(series):
        raise PermissionDenied(_DENIED)
    requested = []
    for pk in transaction_ids:
        if pk not in requested:
            requested.append(pk)
    if not requested:
        raise PermissionDenied(_DENIED)
    eligible = {txn.pk for txn in grouping_candidate_transactions(person)}
    claimed = _active_member_transaction_ids(person, exclude_series_id=series.pk)
    already = set(series.members.values_list("transaction_id", flat=True))
    for pk in requested:
        if pk not in eligible or pk in claimed:
            raise PermissionDenied(_DENIED)
    RecurringExclusion.objects.filter(person=person, transaction_id__in=requested).delete()
    RecurringSeriesMember.objects.bulk_create(
        RecurringSeriesMember(
            series=series,
            transaction_id=pk,
            source=RecurringSeriesMember.Source.MANUAL,
        )
        for pk in requested
        if pk not in already
    )
    return _recompute_series_from_members(series, extra_reasons=(MANUAL_REASON,))


def _lock_confirmed_series(principal, series_id):
    person = _person_for(principal)
    lock_actor_household(person)
    _visible_series(person, series_id)
    series = RecurringSeries.objects.select_for_update(of=("self",)).filter(pk=series_id).first()
    if series is None or series.status != RecurringSeries.Status.CONFIRMED or not series.is_active:
        raise PermissionDenied(_DENIED)
    return person, series


@transaction.atomic
def cancel_recurring_series(principal, series_id):
    _person, series = _lock_confirmed_series(principal, series_id)
    if series.cancelled_at is not None:
        return series
    series.cancelled_at = timezone.now()
    series.save(update_fields=("cancelled_at", "updated_at"))
    return series


@transaction.atomic
def undo_cancel_recurring_series(principal, series_id):
    _person, series = _lock_confirmed_series(principal, series_id)
    if series.cancelled_at is None:
        return series
    series.cancelled_at = None
    series.save(update_fields=("cancelled_at", "updated_at"))
    return series


@transaction.atomic
def confirm_resume_recurring_series(principal, series_id):
    return undo_cancel_recurring_series(principal, series_id)


@transaction.atomic
def keep_cancelled_recurring_series(principal, series_id):
    person, series = _lock_confirmed_series(principal, series_id)
    if series.cancelled_at is None:
        raise PermissionDenied(_DENIED)
    cutoff = timezone.localdate(series.cancelled_at)
    later_ids = list(
        RecurringSeriesMember.objects.filter(series=series, transaction__transaction_date__gt=cutoff).values_list(
            "transaction_id", flat=True
        )
    )
    if not later_ids:
        return series
    for pk in later_ids:
        RecurringExclusion.objects.get_or_create(person=person, transaction_id=pk)
    RecurringSeriesMember.objects.filter(series=series, transaction_id__in=later_ids).delete()
    if not series.members.exists():
        revalidate_series_after_member_removal(person, [series.pk])
        return RecurringSeries.objects.filter(pk=series.pk).first()
    return _recompute_series_from_members(series, extra_reasons=(MANUAL_REASON,))


@transaction.atomic
def dismiss_price_change(principal, series_id):
    _person, series = _lock_confirmed_series(principal, series_id)
    latest = (
        RecurringSeriesMember.objects.filter(series=series)
        .select_related("transaction")
        .order_by("-transaction__transaction_date", "-transaction_id")
        .first()
    )
    if latest is None:
        raise PermissionDenied(_DENIED)
    series.acknowledged_amount_minor = latest.transaction.amount_minor
    series.save(update_fields=("acknowledged_amount_minor", "updated_at"))
    return series
