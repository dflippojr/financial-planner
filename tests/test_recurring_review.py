from datetime import date, datetime, timedelta, timezone as dt_timezone
from hashlib import sha256
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.category_services import ensure_household_categories
from finance.models import (
    Account,
    Alert,
    AlertSettings,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringExclusion,
    RecurringSeries,
    RecurringSeriesMember,
    Transaction,
)
from finance.planning_services import projected_months_for
from finance.recurring_review import (
    is_resumed,
    missed_charge,
    price_change,
    raise_recurring_review_alerts,
    upcoming_charge,
    upcoming_charges,
)
from finance.recurring_services import (
    cancel_recurring_series,
    confirm_resume_recurring_series,
    confirmed_totals,
    dismiss_price_change,
    keep_cancelled_recurring_series,
    undo_cancel_recurring_series,
)


PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 10, 3)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(
    owner,
    *,
    name="Synthetic Checking",
    account_type=Account.Type.CHECKING,
    scope=Account.Scope.PRIVATE,
    household=None,
    share_mode=None,
):
    if share_mode is None:
        share_mode = Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else ""
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 2),
    amount_minor=-1000,
    description="Synthetic row",
    range_start=date(2026, 1, 1),
    range_end=date(2026, 12, 31),
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=sha256(
            f"{account.pk}-{transaction_date}-{amount_minor}-{description}".encode()
        ).hexdigest(),
        date_range_start=range_start,
        date_range_end=range_end,
    )
    digest = sha256(f"txn-{account.pk}-{amount_minor}-{transaction_date}-{description}".encode()).hexdigest()
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def make_series(person, *, name, cadence, typical_minor=-1000, fingerprint=None, cancelled_at=None):
    digest = fingerprint or sha256(f"{person.pk}-{name}-{cadence}".encode()).hexdigest()
    return RecurringSeries.objects.create(
        person=person,
        merchant_key=name.casefold(),
        display_name=name,
        cadence=cadence,
        typical_amount_minor=typical_minor,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=digest,
        cancelled_at=cancelled_at,
    )


def add_member(series, txn):
    return RecurringSeriesMember.objects.create(series=series, transaction=txn)


@pytest.mark.django_db
def test_upcoming_lists_next_dates_for_each_cadence():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    cases = (
        (RecurringSeries.Cadence.WEEKLY, date(2026, 9, 28), date(2026, 10, 5)),
        (RecurringSeries.Cadence.BIWEEKLY, date(2026, 9, 21), date(2026, 10, 5)),
        (RecurringSeries.Cadence.MONTHLY, date(2026, 9, 15), date(2026, 10, 15)),
        (RecurringSeries.Cadence.QUARTERLY, date(2026, 7, 15), date(2026, 10, 15)),
        (RecurringSeries.Cadence.ANNUAL, date(2025, 10, 20), date(2026, 10, 20)),
    )
    series_rows = []
    for cadence, last_on, _expected in cases:
        series = make_series(owner, name=f"Synthetic {cadence}", cadence=cadence)
        add_member(
            series,
            make_transaction(owner, account, transaction_date=last_on, description=f"Synthetic {cadence}"),
        )
        series_rows.append(series)

    upcoming = upcoming_charges(series_rows, today=TODAY)
    found = {item.series.cadence: item.expected_on for item in upcoming}
    for cadence, _last_on, expected in cases:
        assert found[cadence] == expected
    dates = [item.expected_on for item in upcoming]
    assert dates == sorted(dates)


def make_priced_series(owner, account, name, amounts, *, start=date(2025, 1, 10)):
    series = make_series(owner, name=name, cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000)
    for index, amount in enumerate(amounts):
        day = date(start.year + (start.month - 1 + index) // 12, (start.month - 1 + index) % 12 + 1, start.day)
        add_member(
            series,
            make_transaction(owner, account, transaction_date=day, amount_minor=amount, description=name),
        )
    return series


@pytest.mark.django_db
def test_drift_just_under_at_and_over_the_threshold():
    # The boundary is strict (>): a drift of exactly the threshold does not flag.
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    under = make_priced_series(owner, account, "Synthetic Under", (-1000, -1000, -1099))
    at = make_priced_series(owner, account, "Synthetic At", (-1000, -1000, -1100))
    over = make_priced_series(owner, account, "Synthetic Over", (-1000, -1000, -1101))
    drop = make_priced_series(owner, account, "Synthetic Drop", (-1000, -1000, -899))

    assert price_change(under) is None
    assert price_change(at) is None
    change = price_change(over)
    assert change is not None
    assert change.previous_minor == -1000
    assert change.previous_display == "10.00 USD"
    assert change.new_minor == -1101
    assert change.percent_display == "10.1%"
    assert price_change(drop).percent_display == "-10.1%"
    assert price_change(at, 9) is not None
    assert price_change(over, 11) is None


@pytest.mark.django_db
def test_drift_baseline_is_the_median_of_up_to_twelve_prior_charges():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    # One old outlier among the prior charges does not move the median, so the
    # real increase is still measured against $10.00.
    outlier = make_priced_series(owner, account, "Synthetic Outlier", (-5000, -1000, -1000, -1000, -1200))
    change = price_change(outlier)
    assert change is not None
    assert change.previous_minor == -1000
    assert change.percent_display == "20.0%"
    # Only the 12 charges before the latest count: three old $50.00 charges fall
    # out of the window, leaving a $10.00 median.
    windowed = make_priced_series(owner, account, "Synthetic Window", (-5000,) * 3 + (-1000,) * 12 + (-1200,))
    assert price_change(windowed).previous_minor == -1000
    # The latest charge is not part of its own baseline: a steady creep still flags.
    creep = make_priced_series(owner, account, "Synthetic Creep", (-1000, -1000, -1000, -1150))
    assert price_change(creep).previous_minor == -1000
    # An even number of prior charges averages the two middle values.
    even = make_priced_series(owner, account, "Synthetic Even", (-1000, -1100, -1300))
    assert price_change(even).previous_minor == -1050


@pytest.mark.django_db
def test_no_baseline_never_flags():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    single = make_priced_series(owner, account, "Synthetic Single", (-1000, -5000))
    zero = make_priced_series(owner, account, "Synthetic Zero", (0, 0, -5000))
    assert price_change(single) is None
    assert price_change(zero) is None


@pytest.mark.django_db
def test_acknowledged_amount_suppresses_until_the_amount_changes():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    flagged = make_priced_series(owner, account, "Synthetic Rise", (-1000, -1000, -1200))
    assert price_change(flagged) is not None

    dismiss_price_change(owner, flagged.pk)
    flagged.refresh_from_db()
    assert price_change(flagged) is None
    raise_recurring_review_alerts(owner, [flagged], today=TODAY)
    assert not Alert.objects.filter(kind=Alert.Kind.RECURRING_PRICE).exists()
    extra = make_transaction(
        owner, account, transaction_date=date(2026, 10, 10), amount_minor=-1400, description="Synthetic Rise later"
    )
    add_member(flagged, extra)
    again = price_change(flagged)
    assert again is not None
    assert again.new_minor == -1400


@pytest.mark.django_db
def test_household_alert_uses_each_recipient_threshold():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    AlertSettings.objects.create(person=owner, recurring_price_percent=10)
    AlertSettings.objects.create(person=member, recurring_price_percent=25)
    series = make_priced_series(owner, shared, "Synthetic Shared Rise", (-1000, -1000, -1200))

    first = raise_recurring_review_alerts(owner, [series], today=TODAY)
    second = raise_recurring_review_alerts(owner, [series], today=TODAY)

    price_alerts = Alert.objects.filter(kind=Alert.Kind.RECURRING_PRICE)
    assert [alert.recipient_id for alert in first if alert.kind == Alert.Kind.RECURRING_PRICE] == [owner.pk]
    assert list(price_alerts.values_list("recipient_id", "dedupe_key")) == [
        (owner.pk, f"recurring:{series.pk}:-1200")
    ]
    assert second == []
    assert "10.00 USD to 12.00 USD" in price_alerts.get().title


@pytest.mark.django_db
def test_review_page_shows_usual_price_drift_and_viewer_threshold():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    AlertSettings.objects.create(person=owner, recurring_price_percent=20)
    make_priced_series(owner, account, "Synthetic Small Rise", (-1000, -1000, -1150), start=date(2026, 7, 10))
    big = make_priced_series(owner, account, "Synthetic Big Rise", (-1000, -1000, -1240), start=date(2026, 7, 11))
    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("recurring-review"))
    html = page.content.decode()
    assert [item.series.pk for item in page.context["price_changes"]] == [big.pk]
    assert "Usual price" in html
    assert "Drift 24.0% from the usual price" in html
    assert "No price changes over 20%" not in html


@pytest.mark.django_db
def test_missed_charge_shows_missing_import_state():
    owner = make_person("owner")
    make_household(owner)
    missing_account = make_account(owner, name="Missing Account")
    present_account = make_account(owner, name="Present Account")
    missing_series = make_series(owner, name="Synthetic Missing", cadence=RecurringSeries.Cadence.MONTHLY)
    present_series = make_series(owner, name="Synthetic Present", cadence=RecurringSeries.Cadence.MONTHLY)
    add_member(
        missing_series,
        make_transaction(
            owner,
            missing_account,
            transaction_date=date(2026, 8, 15),
            description="Synthetic Missing",
            range_start=date(2026, 8, 1),
            range_end=date(2026, 8, 31),
        ),
    )
    add_member(
        present_series,
        make_transaction(
            owner,
            present_account,
            transaction_date=date(2026, 8, 15),
            description="Synthetic Present",
            range_start=date(2026, 1, 1),
            range_end=date(2026, 12, 31),
        ),
    )

    missing = missed_charge(missing_series, owner, today=TODAY)
    present = missed_charge(present_series, owner, today=TODAY)
    assert missing is not None
    assert missing.expected_on == date(2026, 9, 15)
    assert missing.missing_import is True
    assert present is not None
    assert present.missing_import is False


@pytest.mark.django_db
def test_cancel_leaves_totals_and_projection_and_resume_resolutions_work():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    txn = make_transaction(
        owner, account, transaction_date=date(2026, 9, 10), amount_minor=-2500, description="Synthetic Stream"
    )
    series = make_series(
        owner, name="Synthetic Stream", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-2500
    )
    add_member(series, txn)
    before_months, before_annual = confirmed_totals([series])
    assert before_months > 0
    projected = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert projected[0].spending_minor == 2500

    cancel_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is not None
    assert confirmed_totals([series]) == (0, 0)
    cancelled_projection = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert cancelled_projection[0].spending_minor == 0

    later = make_transaction(
        owner,
        account,
        transaction_date=timezone.localdate() + timedelta(days=14),
        amount_minor=-2500,
        description="Synthetic Stream",
    )
    add_member(series, later)
    assert is_resumed(series) is True
    confirm_resume_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is None
    assert confirmed_totals([series]) == (before_months, before_annual)

    cancel_recurring_series(owner, series.pk)
    keep_cancelled_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is not None
    assert RecurringExclusion.objects.filter(person=owner, transaction=later).exists()
    assert not RecurringSeriesMember.objects.filter(series=series, transaction=later).exists()
    assert is_resumed(series) is False
    undo_cancel_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is None


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
@patch("finance.recurring_services.timezone.localdate", return_value=TODAY)
def test_recurring_page_shows_review_sections_and_actions(_refresh_today, _view_today):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(
        owner, name="Synthetic Monthly", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    add_member(
        series,
        make_transaction(owner, account, transaction_date=date(2026, 9, 15), description="Synthetic Monthly"),
    )
    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("recurring-review"))
    html = page.content.decode()
    assert page.status_code == 200
    assert "Upcoming" in html
    expected_dates = [item.expected_on for item in page.context["upcoming"]]
    assert date(2026, 10, 15) in expected_dates
    assert "Mark cancelled" in html
    # A charge is stored negative and reads as money out; the monthly total is a positive outflow.
    assert "+$10.00" not in html
    assert html.count("\N{MINUS SIGN}$10.00") >= 3

    cancel = client.post(reverse("recurring-review"), {"series_id": series.pk, "action": "cancel"})
    assert cancel.status_code == 302
    series.refresh_from_db()
    cancelled_page = client.get(reverse("recurring-review"))
    cancelled_html = cancelled_page.content.decode()
    assert series.cancelled_at is not None
    assert "Cancelled" in cancelled_html
    assert cancelled_page.context["monthly_minor"] == 0


@pytest.mark.django_db
def test_price_and_missed_alerts_dedupe_and_follow_account_visibility():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private_series = make_series(
        owner, name="Synthetic Private Bill", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    shared_series = make_series(
        owner, name="Synthetic Shared Bill", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    for day, amount in (
        (date(2026, 7, 10), -1000),
        (date(2026, 8, 10), -1000),
        (date(2026, 9, 10), -1200),
    ):
        add_member(
            private_series,
            make_transaction(
                owner,
                private,
                transaction_date=day,
                amount_minor=amount,
                description="Synthetic Private Bill",
            ),
        )
    add_member(
        shared_series,
        make_transaction(
            owner,
            shared,
            transaction_date=date(2026, 8, 15),
            amount_minor=-1000,
            description="Synthetic Shared Bill",
            range_start=date(2026, 8, 1),
            range_end=date(2026, 8, 31),
        ),
    )

    rows = [private_series, shared_series]
    first = raise_recurring_review_alerts(owner, rows, today=TODAY)
    second = raise_recurring_review_alerts(owner, rows, today=TODAY)
    assert second == []
    price_keys = list(
        Alert.objects.filter(kind=Alert.Kind.RECURRING_PRICE).values_list("recipient_id", "dedupe_key")
    )
    missed_keys = list(
        Alert.objects.filter(kind=Alert.Kind.RECURRING_MISSED).values_list("recipient_id", "dedupe_key")
    )
    assert (owner.pk, f"recurring:{private_series.pk}:-1200") in price_keys
    assert (member.pk, f"recurring:{private_series.pk}:-1200") not in price_keys
    assert (owner.pk, f"recurring:{shared_series.pk}:missed:2026-09-15") in missed_keys
    assert (member.pk, f"recurring:{shared_series.pk}:missed:2026-09-15") in missed_keys
    assert len(first) >= 2


@pytest.mark.django_db
def test_member_does_not_see_private_series_review():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    series = make_series(owner, name="Synthetic Secret", cadence=RecurringSeries.Cadence.MONTHLY)
    add_member(
        series,
        make_transaction(owner, private, transaction_date=date(2026, 9, 15), description="Synthetic Secret"),
    )
    client = Client()
    client.force_login(member.user)
    page = client.get(reverse("recurring-review"))
    html = page.content.decode()
    assert "Synthetic Secret" not in html
    forbidden = client.post(reverse("recurring-review"), {"series_id": series.pk, "action": "cancel"})
    assert forbidden.status_code == 404
    series.refresh_from_db()
    assert series.cancelled_at is None


@pytest.mark.django_db
def test_cancelled_series_is_not_upcoming():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(
        owner,
        name="Synthetic Ended",
        cadence=RecurringSeries.Cadence.MONTHLY,
        cancelled_at=datetime(2026, 9, 1, tzinfo=dt_timezone.utc),
    )
    add_member(
        series,
        make_transaction(owner, account, transaction_date=date(2026, 9, 15), description="Synthetic Ended"),
    )
    assert upcoming_charge(series, today=TODAY) is None
    assert missed_charge(series, owner, today=TODAY) is None
    assert price_change(series) is None



@pytest.mark.django_db
def test_overdue_charge_is_due_until_it_posts_or_is_missed():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(owner, name="Synthetic Rent", cadence=RecurringSeries.Cadence.MONTHLY)
    add_member(series, make_transaction(owner, account, transaction_date=date(2026, 9, 12), description="Synthetic Rent"))

    due = upcoming_charge(series, today=date(2026, 10, 13))
    assert due.due is True and due.expected_on == date(2026, 10, 12)
    assert missed_charge(series, owner, today=date(2026, 10, 13)) is None
    # On its expected date it is simply upcoming, not due.
    assert upcoming_charge(series, today=date(2026, 10, 12)).due is False
    # The last tolerated day is still due; the day after, it is missed.
    assert upcoming_charge(series, today=date(2026, 10, 15)).due is True
    assert missed_charge(series, owner, today=date(2026, 10, 15)) is None
    assert upcoming_charge(series, today=date(2026, 10, 16)) is None
    assert missed_charge(series, owner, today=date(2026, 10, 16)).expected_on == date(2026, 10, 12)

    # Once it posts, the next expected charge is a month on and nothing is due.
    add_member(series, make_transaction(owner, account, transaction_date=date(2026, 10, 13), description="Synthetic Rent"))
    assert upcoming_charge(series, today=date(2026, 10, 13)) is None
    assert upcoming_charge(series, today=date(2026, 10, 20)).due is False  # Nov 13 is inside the horizon
