from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.encryption import encrypt_access_url
from finance.lifecycle_services import archive_account
from finance.models import (
    Account,
    AccountLink,
    Household,
    ImportBatch,
    Membership,
    Person,
    SimpleFinConnection,
)


PASSWORD = "Synthetic-passphrase-42!"
CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"
ACCESS_URL = "https://demo:synthetic-access-secret@bridge.example.test/simplefin"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def link_account(owner, account, cutover=date(2026, 3, 1)):
    connection = SimpleFinConnection.objects.create(
        owner=owner,
        encrypted_access_url=encrypt_access_url(ACCESS_URL),
    )
    return AccountLink.objects.create(
        connection=connection,
        account=account,
        simplefin_account_id="CON-1:sf-checking",
        cutover_date=cutover,
        mode=AccountLink.Mode.TRANSACTIONS,
    )


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


@pytest.mark.django_db
def test_import_nav_is_not_the_accounts_page():
    user, _person = make_person("owner")
    client = Client()
    client.force_login(user)
    page = client.get(reverse("csv-import"))
    html = page.content.decode()

    assert page.status_code == 200
    assert reverse("csv-import") == "/imports/"
    assert reverse("csv-import") != reverse("account-list")
    assert f'href="{reverse("csv-import")}"' in html
    assert f'href="{reverse("account-list")}"' in html
    assert 'aria-current="page"' in html
    assert ">Import<" in html


@pytest.mark.django_db
def test_import_page_lists_only_accounts_the_viewer_may_import_into():
    owner_user, owner = make_person("owner")
    member_user, member = make_person("member")
    _outsider_user, outsider = make_person("outsider")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)

    own_checking = Account.objects.create(name="Owner Checking", account_type="checking", owner=owner)
    shared = Account.objects.create(
        name="Household Checking",
        account_type="checking",
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        share_mode=Account.ShareMode.CO_OWNED,
        household=household,
    )
    member_private = Account.objects.create(name="Member Private", account_type="savings", owner=member)
    outsider_private = Account.objects.create(name="Outsider Private", account_type="checking", owner=outsider)
    archived = Account.objects.create(name="Archived Checking", account_type="checking", owner=owner)
    archive_account(owner_user, archived.pk)
    house = Account.objects.create(name="Synthetic House", account_type="real_estate", owner=owner)
    client = Client()
    client.force_login(owner_user)
    page = client.get(reverse("csv-import"))
    html = page.content.decode()

    assert page.status_code == 200
    assert f'value="{own_checking.pk}"' in html
    assert f'value="{shared.pk}"' in html
    assert "Member Private" not in html
    assert "Outsider Private" not in html
    assert "Archived Checking" not in html
    assert "Synthetic House" not in html
    assert reverse("csv-import-preview", args=(own_checking.pk,)) not in html
    assert house.name not in html
    assert member_private.name not in html
    assert outsider_private.name not in html


@pytest.mark.django_db
def test_import_page_marks_simplefin_linked_accounts_and_notes_cutover():
    user, owner = make_person("owner")
    checking = Account.objects.create(name="Linked Checking", account_type="checking", owner=owner)
    savings = Account.objects.create(name="Unlinked Savings", account_type="savings", owner=owner)
    link_account(owner, checking, cutover=date(2026, 3, 11))
    client = Client()
    client.force_login(user)
    page = client.get(reverse("csv-import"))
    html = page.content.decode()

    assert "(Linked)" in html
    assert "2026-03-11" in html
    assert "CSV rows dated before" in html
    assert "Unlinked Savings" in html
    assert html.count("(Linked)") == 1
    assert savings.name in html


@pytest.mark.django_db
def test_import_page_handoff_stages_then_opens_preview(staging_settings):
    user, owner = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=owner)
    client = Client()
    client.force_login(user)
    response = client.post(
        reverse("csv-import"),
        {
            "account_id": str(account.pk),
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )

    assert response.status_code == 302
    assert response.url == reverse("csv-import-preview", args=(account.pk,))
    preview = client.get(response.url)
    assert preview.status_code == 200
    assert b"Map the uploaded columns" in preview.content


@pytest.mark.django_db
def test_import_page_rejects_another_members_private_account_like_missing():
    owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    private = Account.objects.create(name="Owner Private", account_type="checking", owner=owner)
    client = Client()
    client.force_login(viewer_user)
    page = client.get(reverse("csv-import"))
    assert b"Owner Private" not in page.content

    posted = client.post(
        reverse("csv-import"),
        {
            "account_id": str(private.pk),
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )
    missing = client.post(
        reverse("csv-import"),
        {
            "account_id": str(private.pk + 999),
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )
    assert posted.status_code == missing.status_code == 404
    assert posted.content == missing.content


@pytest.mark.django_db
def test_import_page_recent_batches_are_visible_accounts_only():
    owner_user, owner = make_person("owner")
    viewer_user, viewer = make_person("viewer")
    visible = Account.objects.create(name="Owner Checking", account_type="checking", owner=owner)
    hidden = Account.objects.create(name="Viewer Private", account_type="checking", owner=viewer)
    shown = ImportBatch.objects.create(
        account=visible,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="a" * 64,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )
    ImportBatch.objects.create(
        account=hidden,
        imported_by=viewer,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )
    client = Client()
    client.force_login(owner_user)
    page = client.get(reverse("csv-import"))
    html = page.content.decode()

    assert "Owner Checking" in html
    assert "Huntington Bank" in html
    assert reverse("csv-import-undo", args=(visible.pk, shown.pk)) in html
    assert "Viewer Private" not in html
