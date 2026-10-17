"""Cross-view consistency: every view of the same money must agree (#534).

Several pages compute the same money a second time: net worth, the spending
table and its donut, cash flow and the monthly review, the recurring totals,
the projection and the bills calendar. Each bug in this class (#443, #510,
#507, #508, #522, #509) was wrong but internally consistent, so a view-level
example passed. These properties tie each view out against another view of
the same data and against the generated synthetic ledger, never against a
copy of the code under test. A law that an open bug still breaks is
xfail(strict=True) with that bug's number and its shrunk counterexample.
"""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from hypothesis import example, given
from hypothesis import strategies as st

from finance.bills_calendar import build_month, calendar_inputs
from finance.cash_flow import (
    cash_flow_report,
    selected_accounts,
    spending_by_category_report,
    spending_chart_data,
)
from finance.category_services import (
    assign_category,
    confirm_transfer_pair,
    income_and_spending_totals,
    link_refund,
)
from finance.lifecycle_services import archive_account
from finance.models import Account, Category, PlannedItem, RecurringSeries, TransferPair
from finance.monthly_review import review_for_viewer
from finance.months import add_months, month_end
from finance.net_worth import net_worth_report
from finance.planning_services import projected_months_for, save_planned_item, visible_projection_inputs
from finance.snapshot_services import record_manual_snapshot
from finance.year_end import year_end_report
from tests.page_payload import json_script_payload
from tests.test_recurring_review import add_member, make_series
from tests.test_savings_goal_plan import make_confirmed_series

from . import harness as ledger
from .strategies import CAPITAL_ONE, EARLIEST, HUNTINGTON, LATEST, MAX_MINOR, Row, amounts, shifted

pytestmark = [pytest.mark.property, pytest.mark.django_db]

HOUSEHOLD = Account.Scope.HOUSEHOLD
PRIVATE = Account.Scope.PRIVATE
# The reports below run with an explicit "today" after every generated actual.
TODAY = date(2026, 10, 15)
# Four closed months of activity keep each example's reports small.
FIRST_MONTH = date(2026, 6, 1)
LAST_DAY = date(2026, 9, 30)
recent_days = st.dates(FIRST_MONTH, LAST_DAY)


def _months(first=FIRST_MONTH, last=LAST_DAY):
    start = first
    while start <= last:
        yield start, month_end(start)
        start = add_months(start, 1)


def _distinct_sizes(draw, count, *, avoid=()):
    """`count` distinct absolute amounts, none in `avoid`, so no two rows can pair as a transfer."""
    return draw(st.lists(st.integers(1, MAX_MINOR).filter(lambda size: size not in avoid),
                         min_size=count, max_size=count, unique=True))


def _signed(draw, size):
    return size if draw(st.booleans()) else -size


def _row(index, day, amount_minor, label="ROW"):
    return Row(day, amount_minor, f"SYN {label} N{index}", "", f"SYN-{index:05d}")


def _rows_by_description(accounts):
    return dict(ledger.active_rows(accounts).values_list("description", "pk"))


def _period_sums(rows, first, last):
    return ledger.expected_totals([row.amount_minor for row in rows if first <= row.day <= last])


# 1. Net worth: the latest snapshot on or before the date, for every active visible account.

NET_WORTH_TYPES = (
    Account.Type.CHECKING, Account.Type.SAVINGS, Account.Type.INVESTMENT,
    Account.Type.REAL_ESTATE, Account.Type.CREDIT_CARD, Account.Type.LOAN,
)
# docs/requirements.md "Net worth v1" and "Physical assets and loans": credit cards
# and loans are liabilities, and a manual row for one records the amount owed.
LIABILITIES = (Account.Type.CREDIT_CARD, Account.Type.LOAN)


@dataclass(frozen=True)
class Holding:
    owner: str
    shared: bool
    account_type: str
    snapshots: tuple
    archived_on: date | None

    def visible_to(self, viewer):
        return self.shared or self.owner == viewer

    def contribution(self, as_of):
        """What this account adds to net worth on `as_of`, from the generated snapshots."""
        if self.archived_on is not None and as_of >= self.archived_on:
            return 0
        known = [amount for day, amount in self.snapshots if day <= as_of]
        if not known:
            return 0
        return -known[-1] if self.account_type in LIABILITIES else known[-1]


@st.composite
def net_worth_cases(draw):
    holdings = []
    for _ in range(draw(st.integers(1, 5))):
        snapshots = draw(st.dictionaries(st.dates(EARLIEST, LATEST), amounts, max_size=4))
        holdings.append(Holding(
            owner=draw(st.sampled_from(("viewer", "other"))),
            shared=draw(st.booleans()),
            account_type=draw(st.sampled_from(NET_WORTH_TYPES)),
            snapshots=tuple(sorted(snapshots.items())),
            archived_on=draw(st.none() | st.dates(EARLIEST, LATEST)),
        ))
    return tuple(holdings), draw(st.dates(EARLIEST, LATEST))


def _archive_on(person, account, day):
    archive_account(person, account.pk)
    # The service stamps "now"; move it to noon on the generated day, as a past archive would read.
    Account.objects.filter(pk=account.pk).update(
        archived_at=timezone.make_aware(datetime(day.year, day.month, day.day, 12))
    )


@given(net_worth_cases())
def test_net_worth_is_the_latest_snapshot_of_every_active_visible_account(case):
    holdings, as_of = case
    with ledger.isolated():
        people = {"viewer": ledger.make_member("viewer"), "other": ledger.make_member("other")}
        household = ledger.make_household(*people.values())
        for index, holding in enumerate(holdings):
            owner = people[holding.owner]
            account = ledger.make_account(owner, holding.account_type, name=f"Synthetic Holding {index}",
                                          household=household if holding.shared else None)
            for day, amount in holding.snapshots:
                record_manual_snapshot(owner, account.pk, snapshot_date=day, amount_minor=amount)
            if holding.archived_on is not None:
                _archive_on(owner, account, holding.archived_on)

        report = net_worth_report(people["viewer"], date_from=EARLIEST, date_to=as_of, today=TODAY)
        mine = [h for h in holdings if h.visible_to("viewer")]
        assert [period.net_minor for period in report.periods] == [
            sum(h.contribution(period.end) for h in mine) for period in report.periods
        ]
        assert report.periods[-1].end == as_of
        assert report.summary.net_minor == sum(h.contribution(as_of) for h in mine)

        shared = [h for h in holdings if h.shared]
        for person in people.values():
            household_report = net_worth_report(person, date_from=EARLIEST, date_to=as_of, scope=HOUSEHOLD, today=TODAY)
            assert [period.net_minor for period in household_report.periods] == [
                sum(h.contribution(period.end) for h in shared) for period in household_report.periods
            ]


# 2. Category shares: each in [0, 100]%, summing to 100%, and the same as the donut's.

REPORT_MONTH = (date(2026, 8, 1), date(2026, 8, 31))


@dataclass(frozen=True)
class CategoryCase:
    purchases: tuple  # (size, category index or None for Uncategorized, in the report month)
    earlier: tuple  # (size, category index): July purchases, outside the report month
    refunds: tuple  # (size, index into purchases + earlier)
    income: tuple  # unlinked deposits, which are never spending


@st.composite
def category_cases(draw, *, with_refunds):
    category = st.none() | st.integers(0, 3)
    purchase_count = draw(st.integers(1, 6))
    earlier_count = draw(st.integers(0, 2)) if with_refunds else 0
    refund_count = draw(st.integers(0, 3)) if with_refunds else 0
    income_count = draw(st.integers(0, 2))
    sizes = _distinct_sizes(draw, purchase_count + earlier_count + refund_count + income_count)
    purchases = tuple((size, draw(category)) for size in sizes[:purchase_count])
    sizes = sizes[purchase_count:]
    earlier = tuple((size, draw(st.integers(0, 3))) for size in sizes[:earlier_count])
    sizes = sizes[earlier_count:]
    originals = st.integers(0, purchase_count + earlier_count - 1)
    refunds = tuple((size, draw(originals)) for size in sizes[:refund_count])
    return CategoryCase(purchases, earlier, refunds, tuple(sizes[refund_count:]))


def _percent(display):
    return None if display == "—" else Decimal(display.rstrip("%"))


def _check_category_shares(case):
    august = date(2026, 8, 12)
    rows = [_row(i, august, -size, "PURCHASE") for i, (size, _category) in enumerate(case.purchases)]
    rows += [_row(len(rows) + i, date(2026, 7, 12), -size, "EARLIER") for i, (size, _c) in enumerate(case.earlier)]
    refund_rows = [_row(len(rows) + i, august, size, "REFUND") for i, (size, _o) in enumerate(case.refunds)]
    income_rows = [_row(len(rows) + len(refund_rows) + i, august, size, "PAY") for i, size in enumerate(case.income)]
    with ledger.isolated():
        viewer = ledger.make_member("viewer")
        household = ledger.make_household(viewer)
        account = ledger.make_account(viewer, Account.Type.CHECKING, household=household)
        ledger.import_rows(viewer, account, HUNTINGTON, rows + refund_rows + income_rows)
        categories = list(
            Category.objects.filter(household=household, code=Category.Code.CUSTOM).exclude(name="Income").order_by("pk")[:4]
        )
        pks = _rows_by_description([account])
        originals = [*case.purchases, *case.earlier]
        for row, (_size, category) in zip(rows, originals):
            if category is not None:
                assign_category(viewer, pks[row.payee], categories[category].pk)
        for row, (_size, original) in zip(refund_rows, case.refunds):
            link_refund(viewer, pks[row.payee], pks[rows[original].payee])

        # Spending per category, from the generated rows: August purchases, less
        # every August refund in the category of the purchase it refunds.
        expected = Counter()
        for size, category in case.purchases:
            expected[category] += size
        for size, original in case.refunds:
            expected[originals[original][1]] -= size
        report = spending_by_category_report(viewer, date_from=REPORT_MONTH[0], date_to=REPORT_MONTH[1])
        names = {None: "Uncategorized", **{i: category.name for i, category in enumerate(categories)}}
        assert {row.name: row.spending_minor for row in report.rows if row.spending_minor} == {
            names[key]: amount for key, amount in expected.items() if amount
        }
        assert report.total_spending_minor == sum(expected.values())

        chart = spending_chart_data(report)
        positive = [row for row in report.rows if row.spending_minor > 0]
        assert [(row["name"], row["spending_minor"]) for row in chart["chart_rows"]] == [
            (row.name, row.spending_minor) for row in positive
        ]
        donut = {row["name"]: row["share_display"] for row in chart["chart_rows"]}
        for row in report.rows:
            share = _percent(row.percent_display)
            if row.spending_minor > 0:
                assert share is not None and 0 <= share <= 100, row
                assert row.percent_display == donut[row.name]
            else:
                # A net-refund or empty category has no share of spending (#510, owner choice (a)).
                assert share is None or share == 0, row
        if positive:
            total = sum(_percent(row.percent_display) for row in positive)
            # Each share is rounded to 0.1%, so the sum may be off by 0.05% per row.
            assert abs(total - 100) <= Decimal("0.05") * len(positive)


@given(category_cases(with_refunds=False))
def test_category_shares_are_percentages_of_spending_that_match_the_donut(case):
    _check_category_shares(case)


@pytest.mark.xfail(strict=True, reason="#510")
@given(category_cases(with_refunds=True))
# Hypothesis's shrunk counterexample: a 0.03 August refund of a 0.02 July
# purchase leaves its category at -0.03 and net spending at -0.02, so the
# 0.01 Uncategorized purchase shows "-50.0%" in the table.
@example(CategoryCase(purchases=((1, None),), earlier=((2, 0),), refunds=((3, 1),), income=()))
def test_category_shares_stay_within_0_and_100_percent_when_refunds_exceed_spending(case):
    _check_category_shares(case)


# 3. Cash flow: per month and scope, net is income minus spending, and every view agrees.

# Viewer's household checking, viewer's private checking, the other member's
# private checking (never visible to the viewer), and the other member's household card.
CASH_ACCOUNTS = {
    "joint": ("viewer", True, HUNTINGTON),
    "mine": ("viewer", False, HUNTINGTON),
    "theirs": ("other", False, HUNTINGTON),
    "card": ("other", True, CAPITAL_ONE),
}
SCOPE_ACCOUNTS = {"": ("joint", "mine", "card"), HOUSEHOLD: ("joint", "card"), PRIVATE: ("mine",)}


@dataclass(frozen=True)
class CashFlowCase:
    rows: tuple  # (account key, Row)
    files: tuple  # (account key, rows) in import order
    review_month: date


@st.composite
def cash_flow_cases(draw):
    count = draw(st.integers(1, 10))
    sizes = _distinct_sizes(draw, count)
    rows = tuple(
        (draw(st.sampled_from(tuple(CASH_ACCOUNTS))), _row(i, draw(recent_days), _signed(draw, size)))
        for i, size in enumerate(sizes)
    )
    files = []
    for key in CASH_ACCOUNTS:
        mine = [row for where, row in rows if where == key]
        # A statement may arrive as two overlapping files.
        cut = draw(st.integers(0, len(mine)))
        overlap = draw(st.integers(0, cut))
        files += [(key, tuple(part)) for part in (mine[:cut], mine[overlap:]) if part]
    order = draw(st.permutations(files))
    month = draw(st.sampled_from([start for start, _end in _months()]))
    return CashFlowCase(rows, tuple(order), month)


def _cash_world(case_files):
    people = {"viewer": ledger.make_member("viewer"), "other": ledger.make_member("other")}
    household = ledger.make_household(*people.values())
    accounts = {
        key: ledger.make_account(people[owner], shape.account_type, name=f"Synthetic {key.title()}",
                                 household=household if shared else None)
        for key, (owner, shared, shape) in CASH_ACCOUNTS.items()
    }
    for key, rows in case_files:
        owner, _shared, shape = CASH_ACCOUNTS[key]
        ledger.import_rows(people[owner], accounts[key], shape, list(rows))
    return people, accounts


def _month_totals(person, scope, first, last):
    accounts = selected_accounts(person, scope=scope, cash_flow_only=True)
    totals = income_and_spending_totals(person, date_from=first, date_to=last, accounts=accounts)
    return ledger.Totals(totals.income_minor, totals.spending_minor, totals.net_minor)


def _report_by_month(person, scope):
    report = cash_flow_report(person, date_from=FIRST_MONTH, date_to=LAST_DAY, scope=scope, today=TODAY)
    return {period.start: ledger.Totals(period.income_minor, period.spending_minor, period.net_minor)
            for period in report.periods}


def _review_totals(review):
    return ledger.Totals(review.facts["income_minor"], review.facts["spending_minor"], review.facts["net_minor"])


@given(cash_flow_cases())
def test_cash_flow_months_agree_with_totals_the_review_and_the_ledger_in_every_scope(case):
    with ledger.isolated():
        people, _accounts = _cash_world(case.files)
        viewer = people["viewer"]
        for scope, keys in SCOPE_ACCOUNTS.items():
            by_month = _report_by_month(viewer, scope)
            scoped = [row for where, row in case.rows if where in keys]
            for first, last in _months():
                assert by_month[first].net == by_month[first].income - by_month[first].spending
                assert by_month[first] == _month_totals(viewer, scope, first, last) == _period_sums(scoped, first, last)
        assert _report_by_month(people["other"], HOUSEHOLD) == _report_by_month(viewer, HOUSEHOLD)

        review = review_for_viewer(viewer, case.review_month, today=TODAY)
        assert _review_totals(review) == _report_by_month(viewer, "")[case.review_month]


@st.composite
def late_import_cases(draw):
    """A review stored before a statement for its month arrives."""
    case = draw(cash_flow_cases())
    late = _row(len(case.rows), draw(st.dates(case.review_month, month_end(case.review_month))),
                _signed(draw, draw(st.integers(1, MAX_MINOR).filter(
                    lambda size: size not in {abs(row.amount_minor) for _key, row in case.rows}))), "LATE")
    key = draw(st.sampled_from(SCOPE_ACCOUNTS[""]))
    return replace(case, rows=case.rows + ((key, late),), files=case.files + ((key, (late,)),))


def _first_review(case):
    """Everything except the final file is imported before the review is stored."""
    return case.files[:-1], case.files[-1:]


# Owner decision (b) on #509: the stored review stays as it was, but the page says
# "Generated <date>" and "New transactions since this review" when newer rows
# exist for the month. Either the totals agree or the page says why they don't.
@pytest.mark.xfail(strict=True, reason="#509")
@given(late_import_cases())
# Hypothesis's shrunk counterexample: a June review stored with 0.02 of
# spending, then a late 0.01 June charge: the page still says 0.02, cash flow 0.03.
@example(CashFlowCase(
    rows=(("joint", _row(0, date(2026, 6, 1), -2)), ("joint", _row(1, date(2026, 6, 1), -1, "LATE"))),
    files=(("joint", (_row(0, date(2026, 6, 1), -2),)), ("joint", (_row(1, date(2026, 6, 1), -1, "LATE"),))),
    review_month=date(2026, 6, 1),
))
def test_a_stored_review_never_silently_disagrees_with_cash_flow_after_a_late_import(case):
    before, after = _first_review(case)
    with ledger.isolated():
        people, accounts = _cash_world(before)
        viewer = people["viewer"]
        review_for_viewer(viewer, case.review_month, today=TODAY)
        for key, rows in after:
            owner, _shared, shape = CASH_ACCOUNTS[key]
            ledger.import_rows(people[owner], accounts[key], shape, list(rows))

        client = Client()
        client.force_login(viewer.user)
        page = client.get(reverse("monthly-review") + f"?month={case.review_month.isoformat()[:7]}")
        assert page.status_code == 200
        shown = _review_totals(page.context["review"])
        stale_hint = "new transactions since this review" in page.content.decode().lower()
        assert shown == _report_by_month(viewer, "")[case.review_month] or stale_hint


# 4. Recurring: the monthly total is the annual total / 12, to the cent, on every view.

PER_YEAR = {
    RecurringSeries.Cadence.WEEKLY: 52,
    RecurringSeries.Cadence.BIWEEKLY: 26,
    RecurringSeries.Cadence.MONTHLY: 12,
    RecurringSeries.Cadence.QUARTERLY: 4,
    RecurringSeries.Cadence.ANNUAL: 1,
}


@dataclass(frozen=True)
class SeriesCase:
    cadence: str
    typical_minor: int
    status: str
    cancelled: bool
    shared: bool

    @property
    def counted(self):
        return self.status == RecurringSeries.Status.CONFIRMED and not self.cancelled


@st.composite
def recurring_cases(draw):
    count = draw(st.integers(1, 6))
    # Up to a hundred million dollars a charge: 52 a year stays far inside the minor-unit column.
    sizes = draw(st.lists(st.integers(1, 10**10), min_size=count, max_size=count, unique=True))
    return tuple(
        SeriesCase(
            cadence=draw(st.sampled_from(tuple(PER_YEAR))),
            typical_minor=-size,
            status=draw(st.sampled_from((RecurringSeries.Status.CONFIRMED,) * 3 + (
                RecurringSeries.Status.SUGGESTED, RecurringSeries.Status.DISMISSED))),
            cancelled=draw(st.booleans()),
            shared=draw(st.booleans()),
        )
        for size in sizes
    )


def _annual_over_twelve(annual_minor):
    """Annual / 12 to the cent, half to even, as the #507 decision states it."""
    return int((Decimal(annual_minor) / 12).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


@given(recurring_cases())
def test_recurring_monthly_total_is_the_annual_total_over_twelve_on_every_view(cases):
    with ledger.isolated():
        viewer = ledger.make_member("viewer")
        household = ledger.make_household(viewer)
        accounts = {
            False: ledger.make_account(viewer, Account.Type.CHECKING, name="Synthetic Private"),
            True: ledger.make_account(viewer, Account.Type.CHECKING, name="Synthetic Joint", household=household),
        }
        charges = [(case.shared, _row(i, date(2026, 9, 2), case.typical_minor, "BILL")) for i, case in enumerate(cases)]
        for shared, account in accounts.items():
            ledger.import_rows(viewer, account, HUNTINGTON, [row for where, row in charges if where == shared])
        pks = _rows_by_description(list(accounts.values()))
        for case, (_shared, row) in zip(cases, charges):
            series = make_series(
                viewer, name=row.payee, cadence=case.cadence, typical_minor=case.typical_minor,
                cancelled_at=timezone.now() if case.cancelled else None,
            )
            RecurringSeries.objects.filter(pk=series.pk).update(status=case.status)
            add_member(series, ledger.active_rows(list(accounts.values())).get(pk=pks[row.payee]))

        annual = sum(abs(case.typical_minor) * PER_YEAR[case.cadence] for case in cases if case.counted)
        expected = (_annual_over_twelve(annual), annual)

        client = Client()
        client.force_login(viewer.user)
        page = client.get(reverse("recurring-review"))
        payload = json_script_payload(page.content.decode(), "recurring-chart-data")
        assert (page.context["monthly_minor"], page.context["annual_minor"]) == expected
        assert (payload["monthly_minor"], payload["annual_minor"]) == expected
        assert sum(item["annual_minor"] for item in payload["series"]) == annual
        year_end = year_end_report(viewer, year=2026, scope="", today=TODAY).recurring
        assert (year_end.monthly_minor, year_end.annual_minor) == expected


# 5. Projection: each planned occurrence counts once, and day 0 is the latest actual balance.

PLAN_CADENCES = (
    PlannedItem.Cadence.ONE_TIME, PlannedItem.Cadence.WEEKLY,
    PlannedItem.Cadence.BIWEEKLY, PlannedItem.Cadence.MONTHLY,
)
STEP_DAYS = {PlannedItem.Cadence.WEEKLY: 7, PlannedItem.Cadence.BIWEEKLY: 14}
# docs/requirements.md "Early income" (#508): a weekly, biweekly or monthly paycheck may
# post up to 3 days early and still be the expected one; a one-time item only on its day.
EARLY_DAYS = 3


@dataclass(frozen=True)
class Plan:
    kind: str
    amount_minor: int
    cadence: str
    start: date

    @property
    def signed(self):
        return self.amount_minor if self.kind == PlannedItem.Kind.INCOME else -self.amount_minor

    def occurrences(self, first, last):
        """Every date the plan falls on in [first, last], from the cadence definitions alone."""
        if self.cadence == PlannedItem.Cadence.ONE_TIME:
            return [self.start] if first <= self.start <= last else []
        dates, day, months = [], self.start, 0
        while day <= last:
            if day >= first:
                dates.append(day)
            if self.cadence in STEP_DAYS:
                day += timedelta(days=STEP_DAYS[self.cadence])
            else:
                months += 1
                day = add_months(self.start, months).replace(day=self.start.day)
        return dates


@dataclass(frozen=True)
class ProjectionCase:
    snapshot_day: date
    balance_minor: int
    actuals: tuple  # Rows on the checking account, none after today
    plans: tuple
    early: tuple  # (plan index, expected date) whose deposit already posted


@st.composite
def projection_cases(draw):
    plan_count = draw(st.integers(1, 4))
    actual_count = draw(st.integers(0, 5))
    sizes = _distinct_sizes(draw, plan_count + actual_count)
    plans = []
    for size in sizes[:plan_count]:
        cadence = draw(st.sampled_from(PLAN_CADENCES))
        # A monthly plan keeps its day in every month only when the day exists in every month.
        start = draw(st.dates(TODAY - timedelta(days=45), TODAY + timedelta(days=45)).filter(
            lambda day, cadence=cadence: cadence != PlannedItem.Cadence.MONTHLY or day.day <= 28))
        plans.append(Plan(draw(st.sampled_from(PlannedItem.Kind.values)), size, cadence, start))
    snapshot_day = draw(st.dates(TODAY - timedelta(days=30), TODAY))
    actuals = [_row(i, draw(st.dates(snapshot_day - timedelta(days=10), TODAY)), _signed(draw, size), "ACTUAL")
               for i, size in enumerate(sizes[plan_count:])]
    early = []
    for index, plan in enumerate(plans):
        if plan.kind != PlannedItem.Kind.INCOME:
            continue
        tolerance = 0 if plan.cadence == PlannedItem.Cadence.ONE_TIME else EARLY_DAYS
        due = plan.occurrences(TODAY, TODAY + timedelta(days=tolerance))
        if due and draw(st.booleans()):
            posted = draw(st.dates(due[0] - timedelta(days=tolerance), min(due[0], TODAY)))
            actuals.append(_row(len(actuals), posted, plan.amount_minor, "PAYCHECK"))
            early.append((index, due[0]))
    return ProjectionCase(snapshot_day, draw(amounts), tuple(actuals), tuple(plans), tuple(early))


def _save_plan(person, index, plan, *, scope=PRIVATE, replaces=None, name=None):
    return save_planned_item(person, {
        "scope": scope, "name": name or f"Synthetic Plan N{index}", "kind": plan.kind,
        "amount_minor": plan.amount_minor, "start_date": plan.start, "end_date": None,
        "cadence": plan.cadence, "category": None, "replaces_series": replaces,
    })


@given(projection_cases())
def test_each_planned_occurrence_counts_once_and_day_zero_is_the_actual_balance(case):
    with ledger.isolated():
        viewer = ledger.make_member("viewer")
        ledger.make_household(viewer)
        checking = ledger.make_account(viewer, Account.Type.CHECKING)
        record_manual_snapshot(viewer, checking.pk, snapshot_date=case.snapshot_day, amount_minor=case.balance_minor)
        ledger.import_rows(viewer, checking, HUNTINGTON, list(case.actuals))
        saved = [_save_plan(viewer, index, plan) for index, plan in enumerate(case.plans)]

        # The snapshot already holds what posted on or before its day.
        actual = case.balance_minor + sum(row.amount_minor for row in case.actuals if row.day > case.snapshot_day)
        horizon = [add_months(TODAY.replace(day=1), offset) for offset in range(3)]
        for first in horizon[:2]:
            calendar = build_month(viewer, year=first.year, month=first.month, today=TODAY, account_ids=[checking.pk])
            assert calendar.start_balance_minor == actual
            for day in (row for row in calendar.days if row.date >= TODAY):
                expected = {saved[i].pk for i, plan in enumerate(case.plans) if plan.occurrences(day.date, day.date)}
                listed = Counter(item.source_id for item in day.items if item.source == "planned")
                assert listed == Counter(expected), day.date
                skipped = sum(case.plans[i].signed for i, due in case.early if due <= day.date)
                assert day.balance_minor == actual - skipped + sum(
                    len(plan.occurrences(TODAY, day.date)) * plan.signed for plan in case.plans
                ), day.date

        months = projected_months_for(viewer, today=TODAY, horizon=3)
        assert [month.start for month in months] == [add_months(first, 1) for first in horizon]
        for month in months:
            counts = {c.source_id: c.occurrence_count for c in month.contributions if c.source == "planned"}
            assert counts == {
                saved[i].pk: len(plan.occurrences(month.start, month.end))
                for i, plan in enumerate(case.plans) if plan.occurrences(month.start, month.end)
            }
            assert month.net_minor == sum(len(plan.occurrences(month.start, month.end)) * plan.signed
                                          for plan in case.plans)


@dataclass(frozen=True)
class Bill:
    amount_minor: int
    confirmed_by: tuple  # members who confirmed their own copy
    replaced_by: str | None  # the member whose household plan replaces their copy


@st.composite
def bill_cases(draw):
    count = draw(st.integers(1, 3))
    sizes = _distinct_sizes(draw, 2 * count)
    bills = []
    for size in sizes[:count]:
        confirmed = tuple(sorted(draw(st.sets(st.sampled_from(("owner", "member")), min_size=1))))
        bills.append(Bill(size, confirmed, draw(st.none() | st.sampled_from(confirmed))))
    plan_days = tuple(draw(st.integers(1, 28)) for _ in bills)
    return tuple(bills), tuple(sizes[count:]), plan_days


@given(bill_cases())
def test_a_household_bill_counts_once_in_every_members_projection_and_calendar(case):
    bills, plan_sizes, plan_days = case
    with ledger.isolated():
        people = {"owner": ledger.make_member("owner"), "member": ledger.make_member("member")}
        household = ledger.make_household(*people.values())
        joint = ledger.make_account(people["owner"], Account.Type.CHECKING, name="Synthetic Joint", household=household)
        copies, plans = [], {}
        for index, bill in enumerate(bills):
            name = f"Synthetic Bill N{index}"
            first = None
            mine = {}
            for who in bill.confirmed_by:
                first = make_confirmed_series(people[who], name, joint, amount_minor=bill.amount_minor,
                                              same_charges_as=first)
                mine[who] = first
            copies.append(mine)
            if bill.replaced_by is not None:
                plan = Plan(PlannedItem.Kind.EXPENSE, plan_sizes[index], PlannedItem.Cadence.MONTHLY,
                            date(2026, 11, plan_days[index]))
                plans[index] = _save_plan(people[bill.replaced_by], index, plan, scope=HOUSEHOLD,
                                          replaces=mine[bill.replaced_by], name=f"Synthetic Plan N{index}")

        def expected(viewer, scope):
            # A replaced bill is its plan; any other bill counts once, and a
            # combined view holds only the viewer's own copies.
            names = Counter()
            for index, bill in enumerate(bills):
                if index in plans:
                    names[(f"Synthetic Plan N{index}", plan_sizes[index])] += 1
                elif scope == HOUSEHOLD or viewer in bill.confirmed_by:
                    names[(f"Synthetic Bill N{index}", bill.amount_minor)] += 1
            return names

        for viewer, person in people.items():
            for scope in ("", HOUSEHOLD):
                want = expected(viewer, scope)
                inputs = visible_projection_inputs(person, scope=scope)
                assert Counter((item.name, item.amount_minor) for item in inputs) == want
                calendar = calendar_inputs(person, scope=scope, today=TODAY)
                assert Counter((item.name, item.amount_minor) for item in calendar) == want
                for month in projected_months_for(person, today=TODAY, horizon=3, scope=scope):
                    seen = Counter((c.name, c.amount_minor // c.occurrence_count) for c in month.contributions)
                    assert seen == want, month.start
                    assert all(c.occurrence_count == 1 for c in month.contributions)


# 6. Scope: a private account never changes a household-scope total another member sees.

@dataclass(frozen=True)
class ScopeCase:
    joint: tuple  # Rows on the household checking
    private: tuple  # Rows on the owner's private checking, some mirroring a joint row as a transfer
    snapshots: tuple  # (household amount, private amount) on the last day
    plans: tuple  # (household plan, private plan)


@st.composite
def scope_cases(draw):
    count = draw(st.integers(1, 6))
    private_count = draw(st.integers(0, 4))
    sizes = _distinct_sizes(draw, count + private_count + 2)
    joint = [_row(i, draw(recent_days), _signed(draw, size), "JOINT") for i, size in enumerate(sizes[:count])]
    private = [_row(i, draw(recent_days), _signed(draw, size), "PRIVATE")
               for i, size in enumerate(sizes[count:count + private_count])]
    # A transfer between the household and the owner's own account: the private leg mirrors a joint row.
    for row in draw(st.lists(st.sampled_from(joint), unique=True, max_size=len(joint))):
        private.append(_row(len(private), shifted(row.day, draw(st.integers(0, 3))), -row.amount_minor, "MOVE"))
    plans = tuple(
        Plan(draw(st.sampled_from(PlannedItem.Kind.values)), size, PlannedItem.Cadence.MONTHLY, date(2026, 11, 3))
        for size in sizes[-2:]
    )
    return ScopeCase(tuple(joint), tuple(private), (draw(amounts), draw(amounts)), plans)


def _household_views(person):
    """Every household-scope number a member can see."""
    cash = cash_flow_report(person, date_from=FIRST_MONTH, date_to=LAST_DAY, scope=HOUSEHOLD, today=TODAY)
    spending = spending_by_category_report(person, date_from=FIRST_MONTH, date_to=LAST_DAY, scope=HOUSEHOLD)
    worth = net_worth_report(person, date_from=FIRST_MONTH, date_to=TODAY, scope=HOUSEHOLD, today=TODAY)
    projection = projected_months_for(person, today=TODAY, horizon=3, scope=HOUSEHOLD)
    return {
        "cash_flow": [(p.start, p.income_minor, p.spending_minor) for p in cash.periods],
        "spending": sorted((row.name, row.spending_minor, row.percent_display) for row in spending.rows),
        "net_worth": [(p.end, p.net_minor) for p in worth.periods],
        "projection": [(m.start, m.income_minor, m.spending_minor) for m in projection],
    }


@given(scope_cases())
def test_private_activity_never_changes_a_household_total_another_member_sees(case):
    with ledger.isolated():
        owner, member = ledger.make_member("owner"), ledger.make_member("member")
        household = ledger.make_household(owner, member)
        joint = ledger.make_account(owner, Account.Type.CHECKING, name="Synthetic Joint", household=household)
        ledger.import_rows(owner, joint, HUNTINGTON, list(case.joint))
        record_manual_snapshot(owner, joint.pk, snapshot_date=LAST_DAY, amount_minor=case.snapshots[0])
        _save_plan(owner, 0, case.plans[0], scope=HOUSEHOLD)
        before = _household_views(member)
        assert _household_views(owner) == before

        own = ledger.make_account(owner, Account.Type.CHECKING, name="Synthetic Own")
        ledger.import_rows(owner, own, HUNTINGTON, list(case.private))
        for pair in TransferPair.objects.filter(status=TransferPair.Status.SUGGESTED):
            confirm_transfer_pair(owner, pair.pk)
        record_manual_snapshot(owner, own.pk, snapshot_date=LAST_DAY, amount_minor=case.snapshots[1])
        _save_plan(owner, 1, case.plans[1])

        assert _household_views(member) == before
        assert _household_views(owner) == before
