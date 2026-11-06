"""Rules and confirmed recurring series can add a note line to new transactions (#684).

A note is appended below the transaction's own note, once, with a correction-history
row naming its source. Setting or editing the note never changes existing
transactions: those get it only through the preview-then-confirm step, which
bulk edit's undo can reverse.
"""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.auto_notes import NOTE_PREVIEW_CHANGED, clean_note_line, preview_token
from finance.bulk_edit_services import undo_bulk_edit
from finance.export import collect_export_tables
from finance.models import Account, Category, CategoryRule, RecurringSeries, Transaction, TransactionCorrectionHistory
from finance.recurring_services import (
    add_recurring_members,
    apply_recurring_note_to_existing,
    confirm_recurring_series,
    merge_recurring_series,
    note_version,
    recurring_note_preview,
    refresh_recurring_series,
    set_recurring_series_note,
)
from finance.rule_services import (
    RULE_CHANGED_SINCE_PREVIEW,
    apply_enabled_rules_to_transactions,
    apply_rule,
    apply_rule_note_to_existing,
    preview_rule,
    save_category_rule,
)
from tests.test_recurring import add_monthly_charges, make_account, make_household, make_person, make_transaction

NOTE = "Covered by the 50k tire warranty"


def _category(household, name="Groceries"):
    return Category.objects.get(household=household, name=name)


def _rule(person, household, *, note=None, contains="goodyear", owner_kind="personal", rule_id=None, priority=0):
    return save_category_rule(
        person, rule_id=rule_id, owner_kind=owner_kind, description_contains=contains, account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=_category(household).pk, priority=priority,
        note=note,
    )


def _confirmed_rule(person, household, **kwargs):
    rule = _rule(person, household, **kwargs)
    apply_rule(person, rule.pk)
    return CategoryRule.objects.get(pk=rule.pk)


def _note_history(txn):
    return list(
        TransactionCorrectionHistory.objects.filter(transaction=txn, field_name=TransactionCorrectionHistory.Field.NOTE)
        .order_by("pk").values_list("previous_description", "new_description")
    )


def _with_note(txn, note):
    Transaction.objects.filter(pk=txn.pk).update(note=note)
    txn.refresh_from_db()
    return txn


# --- Rules -----------------------------------------------------------------------


def test_a_note_line_is_one_line():
    assert clean_note_line("  Front tires\n warranty \t claim ") == "Front tires warranty claim"


@pytest.mark.django_db
def test_a_new_imported_row_gets_the_rules_note_below_its_own_note_once():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    rule = _confirmed_rule(owner, household, note=NOTE)
    fresh = _with_note(make_transaction(owner, account, description="GOODYEAR SYN 0042"), "Paid with the gift card")

    apply_enabled_rules_to_transactions(owner, [fresh])
    apply_enabled_rules_to_transactions(owner, [fresh])  # a second sync of the same row

    fresh.refresh_from_db()
    assert fresh.note == f"Paid with the gift card\n{NOTE}"
    assert fresh.category_id == rule.category_id
    assert _note_history(fresh) == [
        ("Paid with the gift card", f"Paid with the gift card\n{NOTE} (rule: goodyear)"),
    ]


@pytest.mark.django_db
def test_a_note_that_would_pass_the_limit_is_skipped_and_the_import_still_categorizes():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    _confirmed_rule(owner, household, note=NOTE)
    full = _with_note(make_transaction(owner, account, description="GOODYEAR SYN 0043"), "y" * 1_990)

    apply_enabled_rules_to_transactions(owner, [full])

    full.refresh_from_db()
    assert full.note == "y" * 1_990
    assert full.category_id == _category(household).pk
    assert _note_history(full) == []


@pytest.mark.django_db
def test_applying_a_rule_or_editing_its_note_never_changes_existing_notes():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    old = make_transaction(owner, account, description="GOODYEAR SYN OLD")
    rule = _rule(owner, household, note=NOTE)

    apply_rule(owner, rule.pk)
    _rule(owner, household, rule_id=rule.pk, note="Edited warranty note")

    old.refresh_from_db()
    assert old.category_id == rule.category_id
    assert old.note == ""
    assert _note_history(old) == []


@pytest.mark.django_db
def test_a_note_only_edit_keeps_the_rule_confirmed_but_other_edits_need_a_new_preview():
    owner = make_person("owner")
    household = make_household(owner)
    rule = _confirmed_rule(owner, household)
    assert rule.confirmed_at is not None

    rule = _rule(owner, household, rule_id=rule.pk, note="Line one\nline two")
    assert rule.note == "Line one line two"
    assert rule.confirmed_at is not None

    rule = _rule(owner, household, rule_id=rule.pk, note="Line one line two", priority=5)
    assert rule.confirmed_at is None


@pytest.mark.django_db
def test_saving_without_a_note_keeps_the_stored_note():
    owner = make_person("owner")
    household = make_household(owner)
    rule = _rule(owner, household, note=NOTE)

    rule = _rule(owner, household, rule_id=rule.pk)

    assert rule.note == NOTE


@pytest.mark.django_db
def test_a_rule_note_longer_than_a_note_is_refused():
    owner = make_person("owner")
    household = make_household(owner)

    with pytest.raises(ValidationError):
        _rule(owner, household, note="z" * 2_001)


@pytest.mark.django_db
def test_adding_a_rules_note_to_existing_matches_needs_the_previewed_rows_and_can_be_undone():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    plain = make_transaction(owner, account, description="GOODYEAR SYN 1", amount_minor=-1001)
    noted = _with_note(make_transaction(owner, account, description="GOODYEAR SYN 2", amount_minor=-1002), "Own note")
    already = _with_note(make_transaction(owner, account, description="GOODYEAR SYN 3", amount_minor=-1003), NOTE)
    rule = _rule(owner, household, note=NOTE)
    version = rule.updated_at.isoformat()
    expected = preview_token([plain, noted])

    with pytest.raises(ValidationError, match="changed since the preview"):
        apply_rule_note_to_existing(owner, rule.pk, previewed_version=version, expected_token=preview_token([plain]))
    with pytest.raises(ValidationError, match=RULE_CHANGED_SINCE_PREVIEW):
        apply_rule_note_to_existing(owner, rule.pk, previewed_version="stale", expected_token=expected)
    plain.refresh_from_db()
    assert plain.note == ""

    changed, undo = apply_rule_note_to_existing(owner, rule.pk, previewed_version=version, expected_token=expected)

    assert sorted(txn.pk for txn in changed) == sorted([plain.pk, noted.pk])
    for txn, note in ((plain, NOTE), (noted, f"Own note\n{NOTE}"), (already, NOTE)):
        txn.refresh_from_db()
        assert txn.note == note
    assert _note_history(noted) == [("Own note", f"Own note\n{NOTE} (rule: goodyear)")]
    assert _note_history(already) == []

    undo_bulk_edit(owner, undo.pk)

    for txn, note in ((plain, ""), (noted, "Own note"), (already, NOTE)):
        txn.refresh_from_db()
        assert txn.note == note


@pytest.mark.django_db
def test_rule_page_sets_the_note_previews_existing_rows_and_applies_on_confirm():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    old = make_transaction(owner, account, description="GOODYEAR SYN PAGE")
    rule = _rule(owner, household)
    client = Client()
    client.force_login(owner.user)
    url = reverse("category-rule-detail", args=[rule.pk])

    response = client.post(url, {
        "action": "save", "owner_kind": "personal", "description_contains": "goodyear",
        "rule_action": "categorize", "category": _category(household).pk, "priority": 0, "enabled": "on",
        "note": NOTE,
    })
    assert response.status_code == 302
    rule.refresh_from_db()
    assert rule.note == NOTE
    old.refresh_from_db()
    assert old.note == ""

    page = client.get(url).content.decode()
    assert "Add the note to existing transactions (1)" in page
    assert "Add the note to 1 transaction" in page

    response = client.post(url, {
        "action": "apply_note", "rule_version": rule.updated_at.isoformat(), "preview_token": preview_token([old]),
    })
    assert response.status_code == 302
    old.refresh_from_db()
    assert old.note == NOTE
    page = client.get(url).content.decode()
    assert "Added the note to 1 transaction." in page
    assert reverse("transaction-bulk-undo", args=[client.session["bulk_edit_undo_id"]]) in page


@pytest.mark.django_db
def test_another_member_cannot_add_a_personal_rules_note():
    owner = make_person("owner")
    other = make_person("other")
    household = make_household(owner, other)
    account = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(owner, account, description="GOODYEAR SYN SHARED")
    rule = _rule(owner, household, note=NOTE)

    with pytest.raises(PermissionDenied):
        apply_rule_note_to_existing(other, rule.pk, previewed_version=rule.updated_at.isoformat(), expected_token="")
    client = Client()
    client.force_login(other.user)
    assert client.get(reverse("category-rule-detail", args=[rule.pk])).status_code == 404


@pytest.mark.django_db
def test_a_household_rules_note_reaches_new_shared_rows_for_every_member():
    owner = make_person("owner")
    other = make_person("other")
    household = make_household(owner, other)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    _confirmed_rule(owner, household, note=NOTE, owner_kind="household")
    fresh = make_transaction(other, shared, description="GOODYEAR SYN HOUSE")

    apply_enabled_rules_to_transactions(other, [fresh])

    fresh.refresh_from_db()
    assert fresh.note == NOTE
    _rule_obj, matches = preview_rule(other, CategoryRule.objects.get().pk)
    assert [txn.pk for txn in matches] == [fresh.pk]


# --- Recurring series ---------------------------------------------------------------


def _confirmed_series(owner, account):
    rows = add_monthly_charges(owner, account)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()
    confirm_recurring_series(owner, series.pk)
    return RecurringSeries.objects.get(pk=series.pk), rows


@pytest.mark.django_db
def test_a_new_charge_that_joins_a_confirmed_series_gets_its_note_once():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series, rows = _confirmed_series(owner, account)
    set_recurring_series_note(owner, series.pk, "Family plan, shared with Sam")

    newest = add_monthly_charges(owner, account, count=1, start=date(2026, 4, 15))[0]
    refresh_recurring_series(owner)
    refresh_recurring_series(owner)

    newest.refresh_from_db()
    assert newest.recurring_memberships.get().series_id == series.pk
    assert newest.note == "Family plan, shared with Sam"
    assert _note_history(newest) == [("", "Family plan, shared with Sam (recurring: Synthetic Stream)")]
    for row in rows:
        row.refresh_from_db()
        assert row.note == ""


@pytest.mark.django_db
def test_a_charge_imported_before_the_note_was_set_does_not_get_it_on_its_own():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series, _rows = _confirmed_series(owner, account)
    older = add_monthly_charges(owner, account, count=1, start=date(2026, 4, 15))[0]
    set_recurring_series_note(owner, series.pk, "Family plan")

    refresh_recurring_series(owner)

    older.refresh_from_db()
    assert older.recurring_memberships.get().series_id == series.pk
    assert older.note == ""
    _series, preview = recurring_note_preview(owner, series.pk)
    assert older.pk in {txn.pk for txn in preview}


@pytest.mark.django_db
def test_charges_added_by_hand_or_by_merge_follow_the_same_rule():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series, _rows = _confirmed_series(owner, account)
    old_extra = make_transaction(owner, account, transaction_date=date(2025, 6, 1), amount_minor=-777,
                                 description="SYN ODD CHARGE")
    set_recurring_series_note(owner, series.pk, "Family plan")
    new_extra = make_transaction(owner, account, transaction_date=date(2025, 7, 1), amount_minor=-778,
                                 description="SYN OTHER CHARGE")

    add_recurring_members(owner, series.pk, [old_extra.pk, new_extra.pk])

    old_extra.refresh_from_db()
    new_extra.refresh_from_db()
    assert old_extra.note == ""
    assert new_extra.note == "Family plan"


@pytest.mark.django_db
def test_merging_a_noted_series_into_one_without_a_note_keeps_the_note_and_old_notes():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    source, source_rows = _confirmed_series(owner, account)
    set_recurring_series_note(owner, source.pk, "Family plan")
    add_monthly_charges(owner, account, description="Synthetic Gym", amount_minor=-4000)
    refresh_recurring_series(owner)
    target = RecurringSeries.objects.exclude(pk=source.pk).get()
    confirm_recurring_series(owner, target.pk)

    merge_recurring_series(owner, source.pk, target.pk)

    target.refresh_from_db()
    assert target.note == "Family plan"
    for row in source_rows:
        row.refresh_from_db()
        assert row.note == ""


@pytest.mark.django_db
def test_setting_a_series_note_previews_then_adds_it_to_existing_charges_on_confirm():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series, rows = _confirmed_series(owner, account)
    _with_note(rows[0], "First month free")

    series = set_recurring_series_note(owner, series.pk, "Family plan")
    for row in rows:
        row.refresh_from_db()
    assert [row.note for row in rows] == ["First month free", "", ""]

    _series, preview = recurring_note_preview(owner, series.pk)
    assert sorted(txn.pk for txn in preview) == sorted(row.pk for row in rows)
    with pytest.raises(ValidationError, match=NOTE_PREVIEW_CHANGED):
        apply_recurring_note_to_existing(owner, series.pk, previewed_version=note_version(series),
                                         expected_token=preview_token(rows[:1]))

    _changed, undo = apply_recurring_note_to_existing(
        owner, series.pk, previewed_version=note_version(series), expected_token=preview_token(preview))

    for row in rows:
        row.refresh_from_db()
    assert [row.note for row in rows] == ["First month free\nFamily plan", "Family plan", "Family plan"]
    assert _note_history(rows[0]) == [("First month free", "First month free\nFamily plan (recurring: Synthetic Stream)")]
    undo_bulk_edit(owner, undo.pk)
    for row in rows:
        row.refresh_from_db()
    assert [row.note for row in rows] == ["First month free", "", ""]


@pytest.mark.django_db
def test_an_edited_series_note_needs_a_fresh_preview():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series, rows = _confirmed_series(owner, account)
    previewed = set_recurring_series_note(owner, series.pk, "Family plan")
    version = note_version(previewed)
    RecurringSeries.objects.filter(pk=series.pk).update(note_updated_at=timezone.now())
    RecurringSeries.objects.filter(pk=series.pk).update(note="Individual plan")

    with pytest.raises(ValidationError, match="changed since its preview"):
        apply_recurring_note_to_existing(owner, series.pk, previewed_version=version,
                                         expected_token=preview_token(rows))


@pytest.mark.django_db
def test_only_the_owner_of_a_confirmed_series_can_set_or_apply_its_note():
    owner = make_person("owner")
    other = make_person("other")
    household = make_household(owner, other)
    shared = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    series, _rows = _confirmed_series(owner, shared)

    with pytest.raises(PermissionDenied):
        set_recurring_series_note(other, series.pk, "Not mine")
    with pytest.raises(PermissionDenied):
        apply_recurring_note_to_existing(other, series.pk, previewed_version="", expected_token="")
    client = Client()
    client.force_login(other.user)
    assert client.get(reverse("recurring-note", args=[series.pk])).status_code == 404


@pytest.mark.django_db
def test_a_suggested_series_has_no_note():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get()

    with pytest.raises(PermissionDenied):
        set_recurring_series_note(owner, series.pk, "Too early")


@pytest.mark.django_db
def test_series_note_page_saves_previews_and_applies():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series, rows = _confirmed_series(owner, account)
    client = Client()
    client.force_login(owner.user)
    url = reverse("recurring-note", args=[series.pk])

    assert reverse("recurring-note", args=[series.pk]) in client.get(reverse("recurring-review")).content.decode()
    response = client.post(url, {"action": "save", "note": "Family plan"})
    assert response.status_code == 302
    for row in rows:
        row.refresh_from_db()
        assert row.note == ""
    page = client.get(url).content.decode()
    assert "Add the note to existing transactions (3)" in page

    series.refresh_from_db()
    response = client.post(url, {"action": "apply", "note_version": note_version(series),
                                 "preview_token": preview_token(rows)})
    assert response.status_code == 302
    for row in rows:
        row.refresh_from_db()
        assert row.note == "Family plan"
    page = client.get(url).content.decode()
    assert "Added the note to 3 transactions." in page
    assert "No existing transactions need this note." in page
    assert "Note for new charges: Family plan" in client.get(reverse("recurring-review")).content.decode()


# --- Export ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_export_includes_rule_and_series_notes_for_rules_the_member_can_see():
    owner = make_person("owner")
    other = make_person("other")
    household = make_household(owner, other)
    account = make_account(owner)
    series, _rows = _confirmed_series(owner, account)
    set_recurring_series_note(owner, series.pk, "Family plan")
    mine = _rule(owner, household, note=NOTE)
    shared = _rule(other, household, note="Household line", owner_kind="household", contains="costco")
    _rule(other, household, note="SYN other private line", contains="secret")

    tables = collect_export_tables(owner)

    (series_row,) = tables["recurring_series"]
    assert series_row["note"] == "Family plan" and series_row["note_updated_at"] is not None
    rules = {row["id"]: row for row in tables["category_rules"]}
    assert set(rules) == {mine.pk, shared.pk}
    assert rules[mine.pk]["note"] == NOTE and rules[mine.pk]["owner_kind"] == "personal"
    assert rules[shared.pk]["note"] == "Household line" and rules[shared.pk]["owner_kind"] == "household"
    assert rules[mine.pk]["category_name"] == "Groceries"


def _bulk_matches(owner, account, count, description):
    batch = make_transaction(owner, account, description=f"{description} seed").import_batch
    Transaction.objects.bulk_create([
        Transaction(
            account=account, import_batch=batch, transaction_date=date(2025, 1, 1), amount_minor=-100 - index,
            description=f"{description} {index}", source_row_number=index + 3,
            fingerprint=f"{index:064x}", original_fields={},
        )
        for index in range(count)
    ])


def _submit_apply_form(client, url, page):
    """Post the page's "Add the note" form exactly as a browser would, hidden fields and all."""
    import re

    form = page[page.index('<section class="mb-8" data-auto-note-existing>'):]
    form = form[form.index("<form method=\"post\">"):form.index("</form>")]
    fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    data = {}
    for name, value in fields:
        data.setdefault(name, []).append(value)
    return client.post(url, data)


@pytest.mark.django_db
def test_a_preview_of_more_than_a_thousand_rows_can_still_be_confirmed():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    _bulk_matches(owner, account, 1_005, "GOODYEAR SYN BULK")
    rule = _rule(owner, household, note=NOTE)
    client = Client()
    client.force_login(owner.user)
    url = reverse("category-rule-detail", args=[rule.pk])

    page = client.get(url).content.decode()
    assert "Add the note to existing transactions (1006)" in page
    response = _submit_apply_form(client, url, page)

    assert response.status_code == 302
    assert Transaction.objects.filter(note=NOTE).count() == 1_006


@pytest.mark.django_db
def test_an_untracked_transfer_rule_offers_its_note_to_the_rows_it_already_marked():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    marked = make_transaction(owner, account, description="SYN PAYMENT APP MOVE", amount_minor=-7_000)
    rule = save_category_rule(
        owner, owner_kind="personal", description_contains="payment app", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=None, priority=0,
        action=CategoryRule.Action.UNTRACKED_TRANSFER,
    )
    apply_rule(owner, rule.pk)
    marked.refresh_from_db()
    assert marked.untracked_transfer is True
    rule = save_category_rule(
        owner, rule_id=rule.pk, owner_kind="personal", description_contains="payment app", account_id=None,
        min_amount_minor=None, max_amount_minor=None, category_id=None, priority=0,
        action=CategoryRule.Action.UNTRACKED_TRANSFER, note="Rent share to the landlord app",
    )
    client = Client()
    client.force_login(owner.user)
    url = reverse("category-rule-detail", args=[rule.pk])

    page = client.get(url).content.decode()
    assert "Add the note to existing transactions (1)" in page
    response = _submit_apply_form(client, url, page)

    assert response.status_code == 302
    marked.refresh_from_db()
    assert marked.note == "Rent share to the landlord app"
