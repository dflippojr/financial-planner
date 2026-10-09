from datetime import date

import pytest

from finance.category_services import (
    assign_category,
    confirm_transfer_pair,
    downgrade_unevidenced_auto_marked_pairs,
    income_and_spending_totals,
    refresh_transfer_pairs,
)
from finance.models import Account, Transaction, TransferPair
from finance.pairing_evidence import has_payment_wording
from tests.test_categorization import make_account, make_household, make_person, make_transaction


@pytest.mark.parametrize(
    "text",
    ["Synthetic PAYMENT thank you", "web pmt 123", "AUTOPAY", "ACH deposit internet transfer", "Online Banking xfer",
     "Synthetic e-payment", "CRCARDPMT", "synthetic_pymt_ref", "Payments received", "EPAY 01"],
)
def test_vocabulary_matches(text):
    assert has_payment_wording(text)


@pytest.mark.parametrize("text", ["", None, "Synthetic store", "Synthetic achievement", "Synthetic repayment", "Synthetic cash"])
def test_vocabulary_ignores_other_text(text):
    assert not has_payment_wording(text)


def setup_accounts():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    return owner, household, checking, savings, card


def only_pair(owner):
    refresh_transfer_pairs(owner)
    return TransferPair.objects.get()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("bank_text", "card_text"),
    [("Synthetic autopay", "Synthetic credit"), ("Synthetic debit", "Synthetic payment thank you"),
     ("Synthetic ach debit", "Synthetic generic credit")],
)
def test_card_payment_with_wording_on_either_leg_auto_marks(bank_text, card_text):
    owner, _, checking, _, card = setup_accounts()
    make_transaction(owner, checking, amount_minor=-8800, description=bank_text)
    make_transaction(owner, card, amount_minor=8800, description=card_text)

    pair = only_pair(owner)

    assert pair.status == TransferPair.Status.AUTO_MARKED
    assert pair.kind == TransferPair.Kind.CARD_PAYMENT
    assert "payment or transfer wording on a leg" in pair.reasons


@pytest.mark.django_db
def test_bank_credit_with_card_purchase_stays_suggested_even_with_wording():
    owner, _, checking, _, card = setup_accounts()
    make_transaction(owner, checking, amount_minor=3300, description="Synthetic transfer received")
    make_transaction(owner, card, amount_minor=-3300, description="Synthetic purchase")

    pair = only_pair(owner)

    assert pair.status == TransferPair.Status.SUGGESTED
    assert pair.confidence == TransferPair.Confidence.LOW
    assert any("wrong way" in reason for reason in pair.reasons)
    totals = income_and_spending_totals(owner)
    assert (totals.income_minor, totals.spending_minor) == (3300, 3300)


@pytest.mark.django_db
def test_card_pair_without_wording_stays_suggested_and_says_why():
    owner, _, checking, _, card = setup_accounts()
    make_transaction(owner, checking, amount_minor=-4100, description="Synthetic debit")
    make_transaction(owner, card, amount_minor=4100, description="Synthetic credit")

    pair = only_pair(owner)

    assert pair.status == TransferPair.Status.SUGGESTED
    assert any("no payment or transfer wording" in reason for reason in pair.reasons)
    assert income_and_spending_totals(owner).spending_minor == 4100


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("out_text", "expected"),
    [("Synthetic move out", TransferPair.Status.SUGGESTED), ("Synthetic transfer to savings", TransferPair.Status.AUTO_MARKED)],
)
def test_savings_transfer_needs_wording(out_text, expected):
    owner, _, checking, savings, _ = setup_accounts()
    make_transaction(owner, checking, amount_minor=-2500, description=out_text)
    make_transaction(owner, savings, amount_minor=2500, description="Synthetic move in")

    assert only_pair(owner).status == expected


@pytest.mark.django_db
def test_reevaluation_downgrades_only_failing_pairs_and_restores_categories():
    owner, household, checking, savings, card = setup_accounts()
    groceries = household.categories.get(name="Groceries")
    good_out = make_transaction(owner, checking, amount_minor=-8800, description="Synthetic autopay",
                                transaction_date=date(2026, 1, 2))
    make_transaction(owner, card, amount_minor=8800, description="Synthetic credit", transaction_date=date(2026, 1, 2))
    weak_out = make_transaction(owner, checking, amount_minor=-2500, description="Synthetic transfer out",
                                transaction_date=date(2026, 3, 2))
    weak_in = make_transaction(owner, savings, amount_minor=2500, description="Synthetic transfer in",
                               transaction_date=date(2026, 3, 2))
    confirmed_out = make_transaction(owner, checking, amount_minor=-700, description="Synthetic plain out",
                                     transaction_date=date(2026, 5, 2))
    make_transaction(owner, savings, amount_minor=700, description="Synthetic plain in",
                     transaction_date=date(2026, 5, 2))
    assign_category(owner, weak_out.pk, groceries.pk)
    refresh_transfer_pairs(owner)
    confirm_transfer_pair(owner, TransferPair.objects.get(leg_a=confirmed_out).pk)
    assert TransferPair.objects.filter(status=TransferPair.Status.AUTO_MARKED).count() == 2
    before = income_and_spending_totals(owner)

    # Simulate pairs marked under the old rule: the wording vanishes without a revalidation.
    Transaction.objects.filter(pk__in=(weak_out.pk, weak_in.pk)).update(description="Synthetic plain")

    examined, downgraded = downgrade_unevidenced_auto_marked_pairs()

    assert (examined, downgraded) == (2, 1)
    weak = TransferPair.objects.get(leg_a=weak_out)
    assert weak.status == TransferPair.Status.SUGGESTED
    assert weak.confidence == TransferPair.Confidence.LOW
    assert any("no payment or transfer wording" in reason for reason in weak.reasons)
    weak_out.refresh_from_db()
    assert weak_out.category_id == groceries.pk
    assert TransferPair.objects.get(leg_a=good_out).status == TransferPair.Status.AUTO_MARKED
    assert TransferPair.objects.get(leg_a=confirmed_out).status == TransferPair.Status.CONFIRMED
    after = income_and_spending_totals(owner)
    assert after.spending_minor == before.spending_minor + 2500
    assert after.income_minor == before.income_minor + 2500

    assert downgrade_unevidenced_auto_marked_pairs() == (1, 0)
