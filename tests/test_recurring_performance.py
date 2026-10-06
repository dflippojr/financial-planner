import random
from datetime import date, timedelta
from hashlib import sha256
from time import perf_counter
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse

from finance import recurring_services
from finance.models import Person, RecurringSeries
from finance.recurring_services import detect_recurring_series, refresh_recurring_series
from tests.test_recurring import add_monthly_charges, make_account, make_household, make_person, make_transaction


def _row(pk, day, amount_minor, description="Synthetic Merchant"):
    return SimpleNamespace(
        pk=pk, description=description, transaction_date=day, amount_minor=-amount_minor, currency="USD"
    )


def _random_merchant(count, seed):
    rng = random.Random(seed)
    days = sorted(date(2023, 1, 1) + timedelta(days=rng.randint(0, 1095)) for _ in range(count))
    return [_row(index + 1, day, rng.randint(300, 25000)) for index, day in enumerate(days)]


def test_five_hundred_random_charges_for_one_merchant_finish_quickly():
    rows = _random_merchant(500, seed=1)
    started = perf_counter()
    detect_recurring_series(rows)
    # About 1.5 s locally and about 13 s under CI coverage tracing; the cubic search needed minutes.
    assert perf_counter() - started < 60


def test_frequent_varied_purchases_finish_quickly():
    rng = random.Random(2)
    day = date(2023, 1, 1)
    rows = []
    for pk in range(1, 161):
        day += timedelta(days=rng.randint(3, 9))
        rows.append(_row(pk, day, rng.randint(4000, 18000), "Synthetic Grocer"))
    started = perf_counter()
    detect_recurring_series(rows)
    assert perf_counter() - started < 5


def _golden_digest():
    digest = sha256()
    pk = 0
    for seed in range(60):
        rng = random.Random(seed)
        rows = []
        for merchant in range(rng.randint(1, 4)):
            day = date(2023, 1, 1) + timedelta(days=rng.randint(0, 60))
            base = rng.randint(300, 30000)
            for _ in range(rng.randint(2, 45)):
                kind = seed % 4
                if kind == 0:
                    day += timedelta(days=rng.choice([7, 7, 14, 30, 31, 28, 91]) + rng.randint(-2, 2))
                    amount = base
                elif kind == 1:
                    day += timedelta(days=rng.randint(0, 12))
                    amount = rng.randint(300, 30000)
                elif kind == 2:
                    day += timedelta(days=rng.choice([7, 14, 30]) + rng.randint(-4, 4))
                    amount = int(base * rng.choice([1, 1, 1.1, 1.3, 2]))
                else:
                    day += timedelta(days=rng.randint(0, 3))
                    amount = base + rng.choice([0, 0, 100, 5000])
                pk += 1
                rows.append(_row(pk, day, amount, f"Synthetic M{merchant} store"))
                if rng.random() < 0.1:
                    pk += 1
                    rows.append(_row(pk, day, amount, f"Synthetic M{merchant} store"))
        for item in detect_recurring_series(rows):
            digest.update(
                repr(
                    (
                        item.merchant_key,
                        item.cadence,
                        item.transaction_ids,
                        item.confidence,
                        item.reasons,
                        item.typical_amount_minor,
                        item.fingerprint,
                    )
                ).encode()
            )
    return digest.hexdigest()


def test_detected_series_match_the_ones_found_before_the_search_was_rewritten():
    # Recorded from the previous O(n^3) search over these same synthetic merchants.
    assert _golden_digest() == "e99cbe20bd0bacad076e4cdca0f39b6ae48ac0c31583069ec1353aa03ee7c193"


@pytest.fixture
def household_person():
    owner = make_person("owner")
    make_household(owner)
    return owner, make_account(owner)


@pytest.mark.django_db
def test_page_view_detects_only_when_charges_changed(household_person, monkeypatch):
    owner, account = household_person
    add_monthly_charges(owner, account)
    calls = []
    real = recurring_services.detect_recurring_series_with_skips

    def counting(transactions):
        calls.append(len(transactions))
        return real(transactions)

    monkeypatch.setattr(recurring_services, "detect_recurring_series_with_skips", counting)
    client = Client()
    client.force_login(owner.user)
    assert client.get(reverse("recurring-review")).status_code == 200
    assert client.get(reverse("recurring-review")).status_code == 200
    assert len(calls) == 1
    make_transaction(owner, account, transaction_date=date(2026, 4, 15), amount_minor=-1599, description="Synthetic Stream")
    client.get(reverse("recurring-review"))
    assert len(calls) == 2
    # The explicit refresh always detects.
    refresh_recurring_series(owner)
    assert len(calls) == 3


@pytest.mark.django_db
def test_detection_runs_before_the_household_lock_is_taken(household_person, monkeypatch):
    owner, account = household_person
    add_monthly_charges(owner, account)
    order = []
    real_detect = recurring_services.detect_recurring_series_with_skips
    real_lock = recurring_services.lock_actor_household
    monkeypatch.setattr(
        recurring_services,
        "detect_recurring_series_with_skips",
        lambda rows: (order.append("detect"), real_detect(rows))[1],
    )
    monkeypatch.setattr(
        recurring_services, "lock_actor_household", lambda person: (order.append("lock"), real_lock(person))[1]
    )
    refresh_recurring_series(owner)
    assert order == ["detect", "lock"]


@pytest.mark.django_db
def test_merchant_with_too_many_charges_is_skipped_and_listed(household_person, monkeypatch):
    owner, account = household_person
    add_monthly_charges(owner, account, description="Synthetic Stream", count=6)
    monkeypatch.setattr(recurring_services, "MAX_CHARGES_PER_MERCHANT", 5)
    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("recurring-review"))
    assert b"too many charges" in page.content
    assert b"synthetic stream" in page.content
    assert not RecurringSeries.objects.filter(person=owner).exists()
    assert Person.objects.get(pk=owner.pk).recurring_skipped_merchants == ["synthetic stream"]
