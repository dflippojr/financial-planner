from datetime import date

import pytest
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from finance.models import AuditEvent, RecurringExclusion, RecurringSeries, RecurringSeriesMember, Transaction
from finance.recurring_services import (
    MANUAL_CREATED_REASON,
    ManualSeriesError,
    confirmed_totals,
    create_manual_series,
    list_manual_series_candidates,
    preview_manual_series,
    refresh_recurring_series,
)
from tests.test_recurring_review import make_account, make_household, make_person, make_transaction


def charges(owner, account, days=(2, 3), description="Synthetic Gym", amount=-2500, month_offset=0):
    return [
        make_transaction(
            owner,
            account,
            transaction_date=date(2026, 1 + index + month_offset, day),
            amount_minor=amount,
            description=description,
        )
        for index, day in enumerate(days)
    ]


@pytest.fixture
def owner():
    person = make_person("owner")
    make_household(person)
    return person


@pytest.mark.django_db
def test_two_charges_create_a_confirmed_manual_series(owner):
    account = make_account(owner)
    rows = charges(owner, account)
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    assert series.status == RecurringSeries.Status.CONFIRMED
    assert series.confidence == RecurringSeries.Confidence.LOW
    assert MANUAL_CREATED_REASON in series.reasons
    assert series.typical_amount_minor == -2500
    assert set(series.members.values_list("source", flat=True)) == {RecurringSeriesMember.Source.MANUAL}
    assert confirmed_totals([series]) == (2500, 30000)
    assert AuditEvent.objects.filter(action=AuditEvent.Action.RECURRING_ADDED, target_id=series.pk).exists()


@pytest.mark.django_db
def test_minimum_history_boundary(owner):
    account = make_account(owner)
    rows = charges(owner, account)
    with pytest.raises(ManualSeriesError):
        create_manual_series(owner, "Gym", "monthly", [rows[0].pk])
    with pytest.raises(ManualSeriesError):
        create_manual_series(owner, "Gym", "monthly", [rows[0].pk, rows[0].pk])
    assert not RecurringSeries.objects.exists()
    assert create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows]).pk


@pytest.mark.django_db
def test_irregular_history_is_labelled_but_allowed(owner):
    account = make_account(owner)
    rows = charges(owner, account, days=(2, 20), amount=-1000) + charges(
        owner, account, days=(5,), amount=-9000, month_offset=2
    )
    preview = preview_manual_series(owner, "Odd", "monthly", [row.pk for row in rows])
    assert any("dates" in note for note in preview["notes"])
    assert any("amounts" in note for note in preview["notes"])
    series = create_manual_series(owner, "Odd", "monthly", [row.pk for row in rows])
    assert series.confidence == RecurringSeries.Confidence.LOW
    assert MANUAL_CREATED_REASON in series.reasons
    assert any("irregular" in reason for reason in series.reasons)


@pytest.mark.django_db
def test_invalid_name_and_cadence_rejected(owner):
    rows = charges(owner, make_account(owner))
    ids = [row.pk for row in rows]
    for name, cadence in (("", "monthly"), ("Gym", "daily")):
        with pytest.raises(ManualSeriesError):
            create_manual_series(owner, name, cadence, ids)


@pytest.mark.django_db
def test_refresh_preserves_members_and_does_not_duplicate(owner):
    rows = charges(owner, make_account(owner), days=(2, 3, 2))
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows[:2]])
    refresh_recurring_series(owner)
    assert RecurringSeries.objects.filter(person=owner).count() == 1
    series.refresh_from_db()
    assert series.is_active
    assert series.members.filter(source=RecurringSeriesMember.Source.MANUAL).count() == 2
    assert MANUAL_CREATED_REASON in series.reasons
    assert series.confidence == RecurringSeries.Confidence.LOW


@pytest.mark.django_db
def test_repeat_submission_cannot_duplicate(owner):
    ids = [row.pk for row in charges(owner, make_account(owner))]
    create_manual_series(owner, "Gym", "monthly", ids)
    with pytest.raises(PermissionDenied):
        create_manual_series(owner, "Gym again", "monthly", ids)
    assert RecurringSeries.objects.count() == 1


@pytest.mark.django_db
def test_ineligible_charges_reject_atomically(owner):
    other = make_person("other")
    make_household(other)
    account = make_account(owner)
    good = charges(owner, account)
    hidden = charges(other, make_account(other), description="Hidden")[0]
    positive = make_transaction(owner, account, amount_minor=500, description="Refund")
    transfer = make_transaction(owner, account, amount_minor=-700, description="Transfer")
    Transaction.objects.filter(pk=transfer.pk).update(kind=Transaction.Kind.INVESTMENT_ACTIVITY)
    archived = make_transaction(owner, account, amount_minor=-701, description="Archived")
    Transaction.objects.filter(pk=archived.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())
    claimed_rows = charges(owner, account, description="Claimed", amount=-300)
    create_manual_series(owner, "Claimed", "monthly", [row.pk for row in claimed_rows])
    for bad in (hidden, positive, transfer, archived, claimed_rows[0]):
        with pytest.raises(PermissionDenied):
            create_manual_series(owner, "Gym", "monthly", [good[0].pk, good[1].pk, bad.pk])
    with pytest.raises(PermissionDenied):
        create_manual_series(owner, "Gym", "monthly", [good[0].pk, 999999])
    assert RecurringSeries.objects.count() == 1
    assert not RecurringSeriesMember.objects.filter(transaction__in=good).exists()


@pytest.mark.django_db
def test_picker_is_capped_paginated_and_excludes_claimed(owner):
    account = make_account(owner)
    rows = [
        make_transaction(
            owner, account, transaction_date=date(2026, 1, 1), amount_minor=-100 - i, description=f"Item {i}"
        )
        for i in range(55)
    ]
    page, has_next = list_manual_series_candidates(owner, "", 1)
    assert len(page) == 50 and has_next
    page2, has_next2 = list_manual_series_candidates(owner, "", 2)
    assert len(page2) == 5 and not has_next2
    found, _ = list_manual_series_candidates(owner, "item 7")
    assert [t.description for t in found] == ["Item 7"]
    create_manual_series(owner, "X", "monthly", [rows[0].pk, rows[1].pk])
    ids = {t.pk for t in list_manual_series_candidates(owner, "", 1)[0]}
    assert rows[0].pk not in ids


@pytest.mark.django_db
def test_member_exclusion_is_cleared_when_added_to_a_series(owner):
    rows = charges(owner, make_account(owner))
    RecurringExclusion.objects.create(person=owner, transaction=rows[0])
    series = create_manual_series(owner, "Gym", "monthly", [row.pk for row in rows])
    assert series.members.count() == 2
    assert not RecurringExclusion.objects.exists()


@pytest.mark.django_db
def test_view_preview_then_confirm_without_javascript(owner):
    rows = charges(owner, make_account(owner))
    client = Client()
    client.force_login(owner.user)
    url = reverse("recurring-create")
    page = client.get(url)
    assert page.status_code == 200 and b"Synthetic Gym" in page.content
    data = {"step": "preview", "name": "Gym", "cadence": "monthly", "transaction_id": [r.pk for r in rows]}
    preview = client.post(url, data)
    assert preview.status_code == 200
    assert not RecurringSeries.objects.exists()
    assert b'name="step" value="confirm"' in preview.content
    done = client.post(url, {**data, "step": "confirm"})
    assert done.status_code == 302
    assert RecurringSeries.objects.get().display_name == "Gym"
    assert b"Gym" in client.get(reverse("recurring-review")).content


@pytest.mark.django_db
def test_view_shows_error_for_one_charge_and_404_for_forged_id(owner):
    rows = charges(owner, make_account(owner))
    client = Client()
    client.force_login(owner.user)
    url = reverse("recurring-create")
    one = client.post(url, {"step": "preview", "name": "Gym", "cadence": "monthly", "transaction_id": [rows[0].pk]})
    assert one.status_code == 200 and b"at least 2" in one.content
    forged = client.post(
        url, {"step": "confirm", "name": "Gym", "cadence": "monthly", "transaction_id": [rows[0].pk, 999999]}
    )
    assert forged.status_code == 404
    assert client.post(url, {"step": "bogus"}).status_code == 404
