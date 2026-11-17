"""What counts as income and spending: the one exclusion rule, and the totals built on it.

A transaction is left out of income and spending when it is marked as a
transfer to or from an untracked account, or when it is one leg of an
auto-marked or confirmed transfer or card-payment pair whose legs the viewer
can both see (`exclusion_exists_for`). A linked refund is never income; it
reduces spending in its own stored category. Unverified investment activity is
omitted. Every report, the edit page, bulk edit and the exports read this one
rule, so a new consumer imports it from here rather than re-deriving it. See
"Categories, transfers, and refunds" and "Cash flow over time" in
docs/data-model.md.
"""

from collections import defaultdict
from types import SimpleNamespace

from django.db.models import (
    BooleanField, Case, Count, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce

from .access import require_person as _person_for
from .access import visible_rows
from .models import Account, RefundLink, RefundLinkDrop, Transaction, TransactionSplit, TransferPair


def exclusion_exists_for(principal, *, household_only=False):
    """Whether a row is left out of income and spending as a transfer, as `principal` sees it.

    The one exclusion rule every report uses. A row is left out when it is
    marked as a transfer to or from an untracked account (#642), which holds for
    every viewer because the mark is on the row, or when it is one leg of an
    excluded transfer pair. A row that is both is still one row, so it is left
    out once. By default a pair counts only when both legs are visible to the
    viewer. With `household_only` both legs must sit in household accounts, so a
    household-scope calculation gives every member the same answer: a pair with
    one private leg counts its household leg as income or spending, even for
    the private leg's owner.
    """
    visible = visible_rows(Transaction, principal)
    if household_only:
        visible = visible.filter(account__scope=Account.Scope.HOUSEHOLD)
    visible = visible.values("pk")
    is_pair_leg = Exists(
        TransferPair.objects.excluding_income_and_spending().filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other excluding_income_and_spending calls in the baseline
            Q(leg_a_id=OuterRef("pk")) | Q(leg_b_id=OuterRef("pk")),
            leg_a_id__in=visible,
            leg_b_id__in=visible,
        )
    )
    return ExpressionWrapper(Q(untracked_transfer=True) | Q(is_pair_leg), output_field=BooleanField())


def excluded_transfer_ids(principal):
    """The ids of every visible row `exclusion_exists_for(principal)` leaves out.

    The set form of the same expression, shared by the reports that need the ids
    in Python, so their income and spending cannot drift from the totals.
    """
    return set(
        Transaction.objects.visible_to(principal)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Transaction.objects.visible_to in the baseline
        .filter(exclusion_exists_for(principal))
        .values_list("pk", flat=True)
    )


def with_transfer_state(principal, financial_transaction):
    """Attach the viewer-scoped transfer state to an already loaded transaction."""
    financial_transaction._excluded = (
        Transaction.objects.filter(pk=financial_transaction.pk)
        .annotate(_excluded=exclusion_exists_for(principal))
        .values_list("_excluded", flat=True)
        .first()
        or False
    )
    return financial_transaction


def _totals_base(principal, *, date_from=None, date_to=None, accounts=None, tag=None, q=None):
    """Visible, countable cash-flow rows: no transfer legs, with a refund flag.

    A selection of household accounts only is a household-scope calculation,
    so transfer pairs are judged by their household legs (`exclusion_exists_for`).
    Optional `q` keeps the rows the transaction list's search matches, and
    narrows the visible set like `tag`; it never widens it.
    """
    person = _person_for(principal)
    visible_accounts = visible_rows(Account, person)
    is_transfer_leg = exclusion_exists_for(person)
    links = RefundLink.objects.filter(refund_id=OuterRef("pk"))
    # A link dropped for this person only (#570) is theirs alone, like the pair that dropped it.
    is_refund = Exists(links.exclude(pk__in=RefundLinkDrop.objects.filter(person=person).values("link_id")))
    if accounts is not None:
        visible_accounts = visible_accounts.filter(pk__in=[getattr(item, "pk", item) for item in accounts])
        # Decided inside the query so totals cost no extra round trip.
        not_household_only = Exists(visible_accounts.exclude(scope=Account.Scope.HOUSEHOLD))
        is_transfer_leg = Case(
            When(not_household_only, then=is_transfer_leg),
            default=exclusion_exists_for(person, household_only=True),
            output_field=BooleanField(),
        )
        is_refund = Case(When(not_household_only, then=is_refund), default=Exists(links), output_field=BooleanField())
    transactions = visible_rows(Transaction, person).filter(
        status=Transaction.Status.ACTIVE,
        kind=Transaction.Kind.CASH_FLOW,
        account_id__in=visible_accounts.values("pk"),
    )
    if tag is not None:
        from .tag_services import apply_tag_filter

        transactions = apply_tag_filter(transactions, tag)
    if q:
        from .transaction_filters import search_q

        transactions = transactions.filter(search_q(q))
    if date_from:
        transactions = transactions.filter(transaction_date__gte=date_from)
    if date_to:
        transactions = transactions.filter(transaction_date__lte=date_to)
    return transactions.annotate(
        _is_transfer_leg=is_transfer_leg,
        _is_refund=is_refund,
    ).filter(_is_transfer_leg=False)


def spending_account_ids(principal, *, date_from, date_to, accounts=None):
    """Accounts holding a row that `income_and_spending_by_window` counts as spending in the range.

    The same rows as the totals: no transfer legs or untracked transfers, and a
    refund counts wherever it sits.
    """
    base = _totals_base(principal, date_from=date_from, date_to=date_to, accounts=accounts)
    rows = base.filter(Q(_is_refund=True) | Q(amount_minor__lt=0)).order_by()
    return set(rows.values_list("account_id", flat=True).distinct())


def _income_spending_sums():
    """Aggregates for one group of `_totals_base` rows: a refund reduces spending."""
    plain = Q(_is_refund=False)
    return {
        "income": Coalesce(Sum("amount_minor", filter=plain & Q(amount_minor__gt=0)), 0),
        "spending": Coalesce(
            Sum(-F("amount_minor"), filter=plain & Q(amount_minor__lt=0)), 0
        )
        + Coalesce(Sum(-F("amount_minor"), filter=Q(_is_refund=True)), 0),
    }


def income_and_spending_totals(principal, *, date_from=None, date_to=None, accounts=None, tag=None):
    """Access-filtered income and spending for later cash-flow views.

    Transfers are excluded only when both legs are visible, and, when every
    selected account is a household account, only when both legs are in
    household accounts. Linked refunds are
    never income; they reduce spending from the refund's own stored category and
    amount even when the original purchase is no longer visible. Unverified
    investment activity is omitted. Optional `accounts` must already be visible;
    ids the viewer cannot see are dropped rather than queried. Optional `tag`
    narrows the already-visible set and never widens it.

    Computed with grouped SQL aggregates; no transaction is built as an object.
    """
    base = _totals_base(principal, date_from=date_from, date_to=date_to, accounts=accounts, tag=tag)
    sums = base.aggregate(**_income_spending_sums())
    income = sums["income"]
    spending = sums["spending"]

    by_category = _category_sums(base)
    spending_by_category = {cat: spend for (_, cat), (_, spend, _, spend_rows) in by_category.items() if spend_rows}
    income_by_category = {cat: inc for (_, cat), (inc, _, inc_rows, _) in by_category.items() if inc_rows}
    return SimpleNamespace(
        income_minor=income,
        spending_minor=spending,
        net_minor=income - spending,
        spending_by_category_id=spending_by_category,
        income_by_category_id=income_by_category,
    )


def _category_sums(base, windows=None):
    """`{(bucket, category_id): (income, spending, income_rows, spending_rows)}`.

    A split transaction contributes its parts' categories; one flagged split
    with no parts falls back to its own category, as an unsplit one does. The
    bucket is the index of the window holding the date, or 0 without windows.
    """
    has_parts = Exists(TransactionSplit.objects.filter(transaction_id=OuterRef("pk")))
    split_rows = Q(category_source=Transaction.CategorySource.SPLIT, _has_parts=True, _is_refund=False)
    whole = base.annotate(_has_parts=has_parts)
    sums = defaultdict(lambda: [0, 0, 0, 0])

    def add(bucket, category_id, income, spending, income_rows, spending_rows):
        entry = sums[(bucket, category_id)]
        entry[0] += income
        entry[1] += spending
        entry[2] += income_rows
        entry[3] += spending_rows

    plain = whole.exclude(split_rows).annotate(bucket=_bucket(windows, "transaction_date"))
    for row in plain.values("bucket", "category_id").annotate(
        income=Coalesce(Sum("amount_minor", filter=Q(_is_refund=False, amount_minor__gt=0)), 0),
        spending=Coalesce(Sum(-F("amount_minor"), filter=Q(_is_refund=False, amount_minor__lt=0)), 0)
        + Coalesce(Sum(-F("amount_minor"), filter=Q(_is_refund=True)), 0),
        income_rows=Count("pk", filter=Q(_is_refund=False, amount_minor__gt=0)),
        spending_rows=Count("pk", filter=Q(_is_refund=True) | Q(amount_minor__lt=0)),
    ):
        add(row["bucket"], row["category_id"], row["income"], row["spending"], row["income_rows"], row["spending_rows"])
    parts = TransactionSplit.objects.filter(transaction_id__in=whole.filter(split_rows).values("pk"))
    for row in (
        parts.annotate(bucket=_bucket(windows, "transaction__transaction_date"))
        .values("bucket", "category_id")
        .annotate(
            income=Coalesce(Sum("amount_minor", filter=Q(transaction__amount_minor__gt=0)), 0),
            spending=Coalesce(Sum(-F("amount_minor"), filter=Q(transaction__amount_minor__lt=0)), 0),
            income_rows=Count("pk", filter=Q(transaction__amount_minor__gt=0)),
            spending_rows=Count("pk", filter=Q(transaction__amount_minor__lt=0)),
        )
    ):
        add(row["bucket"], row["category_id"], row["income"], row["spending"], row["income_rows"], row["spending_rows"])
    return {key: tuple(value) for key, value in sums.items() if key[0] >= 0}


def _bucket(windows, field):
    if windows is None:
        return Value(0, output_field=IntegerField())
    return Case(
        *(
            When(**{f"{field}__gte": start, f"{field}__lte": end}, then=Value(index))
            for index, (start, end) in enumerate(windows)
        ),
        default=Value(-1),
        output_field=IntegerField(),
    )


def spending_by_category_by_window(principal, windows, *, accounts=None, tag=None):
    """Per-window `(spending_minor, {category_id: spending_minor})`, two queries in all.

    Windows must not overlap. The same rules as `income_and_spending_totals`.
    """
    windows = list(windows)
    if not windows:
        return []
    base = _totals_base(
        principal,
        date_from=min(start for start, _ in windows),
        date_to=max(end for _, end in windows),
        accounts=accounts,
        tag=tag,
    )
    categories = [{} for _ in windows]
    for (bucket, category_id), (_, spending, _, spending_rows) in _category_sums(base, windows).items():
        if spending_rows:
            categories[bucket][category_id] = spending
    return [(sum(item.values()), item) for item in categories]


def income_and_spending_by_account(principal, accounts, *, date_from=None, date_to=None):
    """`{account_id: (income_minor, spending_minor)}` for the given visible accounts."""
    base = _totals_base(principal, date_from=date_from, date_to=date_to, accounts=accounts)
    grouped = {
        row["account_id"]: (row["income"], row["spending"])
        for row in base.values("account_id").annotate(**_income_spending_sums())
    }
    return {getattr(item, "pk", item): grouped.get(getattr(item, "pk", item), (0, 0)) for item in accounts}


def income_and_spending_by_tag(principal, tags, *, date_from=None, date_to=None, accounts=None):
    """`{tag_id: (income_minor, spending_minor)}`; a transaction counts under each of its tags."""
    base = _totals_base(principal, date_from=date_from, date_to=date_to, accounts=accounts)
    wanted = [getattr(item, "pk", item) for item in tags]
    grouped = {
        row["tag_id"]: (row["income"], row["spending"])
        for row in base.filter(tags__in=wanted)
        .annotate(tag_id=F("tags"))
        .values("tag_id")
        .annotate(**_income_spending_sums())
    }
    return {tag_id: grouped.get(tag_id, (0, 0)) for tag_id in wanted}


def income_and_spending_by_window(principal, windows, *, accounts=None, tag=None, q=None):
    """Income and spending for each `(start, end)` window in one grouped query.

    Windows must not overlap. Returns one `(income_minor, spending_minor)` pair
    per window, in order, with zeros for windows that have no activity. Optional
    `q` counts only the rows the transaction list's search matches.
    """
    windows = list(windows)
    if not windows:
        return []
    base = _totals_base(
        principal,
        date_from=min(start for start, _ in windows),
        date_to=max(end for _, end in windows),
        accounts=accounts,
        tag=tag,
        q=q,
    )
    bucket = Case(
        *(
            When(transaction_date__gte=start, transaction_date__lte=end, then=Value(index))
            for index, (start, end) in enumerate(windows)
        ),
        default=Value(-1),
        output_field=IntegerField(),
    )
    totals = {
        row["bucket"]: (row["income"], row["spending"])
        for row in base.annotate(bucket=bucket).values("bucket").annotate(**_income_spending_sums())
    }
    return [totals.get(index, (0, 0)) for index in range(len(windows))]
