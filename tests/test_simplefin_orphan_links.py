"""Saved SimpleFIN links whose account the latest read no longer reports (#736).

Connections lists them under "No longer reported by SimpleFIN" so the member can
keep or unlink them. Unlink is the same delete as Ignore: only the AccountLink
goes, the transactions stay. A failed read lists none of them, so a provider
outage never makes a link look orphaned. All data is synthetic.
"""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from finance.audit_models import AuditEvent
from finance.models import Account, AccountLink, SimpleFinConnection, Transaction
from finance.simplefin_errors import SimpleFinError
from finance.simplefin_services import listed_accounts, store_read, sync_connection
from tests.test_simplefin import (
    _linked_owner_and_checking,
    account_payload,
    make_account,
    make_household,
    make_person,
    posted_txn,
    signed_in,
)

HEADING = "No longer reported by SimpleFIN"
LINKED_ID = "CON-1:sf-checking"
CUTOVER = date(2026, 3, 1)
COVERED = "Covered by SimpleFIN from 2026-03-01."


@pytest.fixture
def staging(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def _store_read_without_link(connection):
    """A successful read that reports a different account than the linked one."""
    payload = account_payload(account_id="sf-other")
    payload["accounts"][0]["name"] = "Synthetic Other Account"
    store_read(connection.pk, payload, listed_accounts(payload), timezone.now())


def _orphaned(monkeypatch, **kwargs):
    payload = account_payload(transactions=[posted_txn(txn_id="sf-1", day=10, amount="-5.00")])
    owner, checking, connection = _linked_owner_and_checking(monkeypatch, payload=payload, cutover=CUTOVER, **kwargs)
    if "owner" not in kwargs:
        make_household(owner)
    sync_connection(owner, connection.pk, ignore_rate_limit=True)
    _store_read_without_link(connection)
    return owner, checking, connection


def _orphan_section(page):
    html = page.content.decode()
    assert HEADING in html, "the orphaned-links section is missing"
    return html[html.index(HEADING):]


def _post_orphans(owner, *actions):
    form = {"intent": "orphans"}
    for index, (simplefin_account_id, action) in enumerate(actions):
        form[f"orphan_id_{index}"] = simplefin_account_id
        form[f"orphan_action_{index}"] = action
    return signed_in(owner).post(reverse("simplefin-connections"), form)


@pytest.mark.django_db
def test_an_orphaned_link_is_listed_with_its_account_last_sync_and_cutover(monkeypatch):
    owner, _checking, _connection = _orphaned(monkeypatch)

    section = _orphan_section(signed_in(owner).get(reverse("simplefin-connections")))

    assert "Synthetic Checking" in section
    assert "2026-03-10" in section  # The newest synced transaction.
    assert "2026-03-01" in section  # The cut-over.
    assert 'value="keep"' in section
    assert 'value="unlink"' in section
    assert f'value="{LINKED_ID}"' in section


@pytest.mark.django_db
def test_unlink_deletes_only_the_link_and_keeps_the_transactions(monkeypatch):
    owner, checking, connection = _orphaned(monkeypatch)
    synced = Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).count()
    assert synced == 1

    response = _post_orphans(owner, (LINKED_ID, "unlink"))

    assert response.status_code == 302
    assert not AccountLink.objects.filter(connection=connection).exists()
    assert Account.objects.filter(pk=checking.pk, status=Account.Status.ACTIVE).exists()
    assert Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).count() == synced
    assert AuditEvent.objects.filter(
        action=AuditEvent.Action.SIMPLEFIN_LINKS_CHANGED, target_id=connection.pk, changed_fields=["account_links"]
    ).exists()
    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()
    assert HEADING not in page


@pytest.mark.django_db
def test_keep_leaves_the_link_as_it_is(monkeypatch):
    owner, checking, connection = _orphaned(monkeypatch)

    _post_orphans(owner, (LINKED_ID, "keep"))

    link = AccountLink.objects.get(connection=connection)
    assert (link.account_id, link.cutover_date) == (checking.pk, CUTOVER)


@pytest.mark.django_db
def test_a_failed_read_lists_no_orphans_and_refuses_unlink(monkeypatch):
    owner, _checking, connection = _orphaned(monkeypatch)
    # No stored read, and the fresh read fails: load_error is set.
    SimpleFinConnection.objects.filter(pk=connection.pk).update(last_read={})

    def outage(*args, **kwargs):
        raise SimpleFinError("SimpleFIN could not be reached. Try again later.")

    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", outage)

    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()
    assert "SimpleFIN could not be reached" in page
    assert HEADING not in page

    _post_orphans(owner, (LINKED_ID, "unlink"))

    assert AccountLink.objects.filter(connection=connection, simplefin_account_id=LINKED_ID).exists()


@pytest.mark.django_db
def test_a_link_present_in_the_read_is_not_listed_or_changed(monkeypatch):
    payload = account_payload(transactions=[posted_txn(txn_id="sf-1", day=10, amount="-5.00")])
    owner, checking, connection = _linked_owner_and_checking(monkeypatch, payload=payload, cutover=CUTOVER)
    make_household(owner)
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()
    assert HEADING not in page

    _post_orphans(owner, (LINKED_ID, "unlink"))

    link = AccountLink.objects.get(connection=connection)
    assert (link.account_id, link.cutover_date, link.simplefin_account_id) == (checking.pk, CUTOVER, LINKED_ID)


@pytest.mark.django_db
def test_an_orphaned_link_to_an_account_no_longer_visible_does_not_show_its_name(monkeypatch):
    member = make_person("member")
    sharer = make_person("sharer")
    household = make_household(member, sharer)
    shared = make_account(sharer, name="Synthetic Sharer Account", scope=Account.Scope.HOUSEHOLD, household=household)
    _member, _shared, connection = _orphaned(monkeypatch, owner=member, account=shared)
    # Unsharing releases the link (#450); a direct update stands in for any other way the
    # member could lose sight of a still-linked account.
    Account.objects.filter(pk=shared.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")

    section = _orphan_section(signed_in(member).get(reverse("simplefin-connections")))

    assert "Synthetic Sharer Account" not in section
    assert "An account you can no longer see" in section
    assert "2026-03-10" not in section
    assert AccountLink.objects.filter(connection=connection).exists()


CSV = (
    b"When,Memo,Amount,Currency\n"
    b"03/16/2026,SYNTHETIC AFTER CUTOVER,-3.00,USD\n"
)


def _csv_preview(owner, account):
    client = signed_in(owner)
    url = reverse("csv-import-preview", args=(account.pk,))
    token = client.post(url, {
        "action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
    }).context["mapping_form"].initial["token"]
    fields = {
        "token": token, "date_column": "When", "description_column": "Memo",
        "date_format": "mdy_slash_4", "number_format": "dot_comma", "amount_mode": "signed",
        "amount_column": "Amount", "debit_column": "", "credit_column": "", "currency_column": "Currency",
    }
    return client.post(url, {**fields, "action": "preview"}).context["preview"]


@pytest.mark.django_db
def test_after_unlink_a_csv_import_is_no_longer_refused_by_the_old_cutover(staging, monkeypatch):
    owner, checking, _connection = _orphaned(monkeypatch)
    [refused] = _csv_preview(owner, checking).rows
    assert refused.overlap_status == "invalid"
    assert COVERED in refused.errors

    _post_orphans(owner, (LINKED_ID, "unlink"))

    [accepted] = _csv_preview(owner, checking).rows
    assert accepted.overlap_status == "new"
    assert COVERED not in accepted.errors
