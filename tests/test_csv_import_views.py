import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from finance.csv_import.parser import MAX_FILE_BYTES
from finance.csv_import.staging import SESSION_KEY
from finance.lifecycle_services import archive_account
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


PASSWORD = "Synthetic-passphrase-42!"
CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def upload(client, account, content=CSV):
    return client.post(
        reverse("csv-import-preview", args=(account.pk,)),
        {"action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", content, "text/csv")},
    )


def mapping_data(token, **changes):
    data = {
        "action": "preview",
        "token": token,
        "date_column": "When",
        "description_column": "Memo",
        "date_format": "mdy_slash_4",
        "number_format": "dot_comma",
        "amount_mode": "signed",
        "amount_column": "Amount",
        "debit_column": "",
        "credit_column": "",
        "currency_column": "Currency",
    }
    data.update(changes)
    return data


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


@pytest.mark.django_db
def test_preview_requires_authentication(staging_settings):
    _user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    response = Client().get(reverse("csv-import-preview", args=(account.pk,)))
    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_missing_and_private_unauthorized_accounts_are_indistinguishable(staging_settings):
    owner_user, owner = make_person("owner")
    viewer_user, _viewer = make_person("viewer")
    private = Account.objects.create(name="Owner Private", account_type="checking", owner=owner)
    client = Client()
    client.force_login(viewer_user)

    private_response = client.get(reverse("csv-import-preview", args=(private.pk,)))
    missing_response = client.get(reverse("csv-import-preview", args=(private.pk + 999,)))

    assert private_response.status_code == missing_response.status_code == 404
    assert private_response.content == missing_response.content


@pytest.mark.django_db
def test_archived_account_preview_is_missing_and_home_omits_its_import_link(staging_settings):
    owner_user, owner = make_person("owner")
    _viewer_user, viewer = make_person("viewer")
    active = Account.objects.create(name="Synthetic Active", account_type="checking", owner=owner)
    archived = Account.objects.create(name="Synthetic Archived", account_type="savings", owner=owner)
    foreign = Account.objects.create(name="Viewer Private", account_type="checking", owner=viewer)
    archive_account(owner_user, archived.pk)
    client = Client()
    client.force_login(owner_user)
    preview_url = reverse("csv-import-preview", args=(archived.pk,))

    get_response = client.get(preview_url)
    upload_response = upload(client, archived)
    map_response = client.post(preview_url, mapping_data("unused-token"))
    cancel_response = client.post(preview_url, {"action": "cancel", "token": "unused-token"})
    missing_response = client.get(reverse("csv-import-preview", args=(archived.pk + 999,)))
    foreign_response = client.get(reverse("csv-import-preview", args=(foreign.pk,)))
    active_get = client.get(reverse("csv-import-preview", args=(active.pk,)))
    home = client.get(reverse("home"))

    assert get_response.status_code == missing_response.status_code == 404
    assert get_response.content == missing_response.content
    assert upload_response.status_code == map_response.status_code == cancel_response.status_code == 404
    assert foreign_response.status_code == 404
    assert active_get.status_code == 200
    assert reverse("csv-import-preview", args=(active.pk,)).encode() in home.content
    assert preview_url.encode() not in home.content
    assert b"Synthetic Archived" in home.content


@pytest.mark.django_db
def test_current_household_member_can_preview_shared_account(staging_settings):
    _owner_user, owner = make_person("owner")
    viewer_user, viewer = make_person("viewer")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=viewer, household=household)
    account = Account.objects.create(
        name="Shared Card", account_type="credit_card", owner=owner,
        scope="household", household=household,
    )
    client = Client()
    client.force_login(viewer_user)

    response = upload(client, account)

    assert response.status_code == 200
    assert response.context["mapping_form"]


@pytest.mark.django_db
def test_upload_stages_privately_then_previews_without_database_writes(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)

    staged = upload(client, account)
    token = staged.context["mapping_form"].initial["token"]
    stage_path = Path(staging_settings) / f"{token}.csvstage"
    assert stage_path.read_bytes() == CSV

    response = client.post(reverse("csv-import-preview", args=(account.pk,)), mapping_data(token))

    assert response.status_code == 200
    assert response.context["preview"].valid_count == 1
    assert response.context["preview"].rows[0].amount_minor == -1234
    assert b"negative for money out" in response.content
    assert b"This tool previews data only" in response.content
    assert not ImportBatch.objects.exists()
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_separate_columns_and_inverted_signed_amounts_are_available(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    separate_csv = b"When,Memo,Debit,Credit\n09/27/2026,SYNTHETIC FOOD,12.34,\n09/28/2026,SYNTHETIC PAY,,50.00\n"
    staged = upload(client, account, separate_csv)
    token = staged.context["mapping_form"].initial["token"]
    data = mapping_data(
        token, amount_mode="separate", amount_column="", debit_column="Debit",
        credit_column="Credit", currency_column="",
    )
    response = client.post(reverse("csv-import-preview", args=(account.pk,)), data)
    assert [row.amount_minor for row in response.context["preview"].rows] == [-1234, 5000]

    signed = upload(client, account)
    signed_token = signed.context["mapping_form"].initial["token"]
    inverted = client.post(
        reverse("csv-import-preview", args=(account.pk,)), mapping_data(signed_token, invert_sign="on")
    )
    assert inverted.context["preview"].rows[0].amount_minor == 1234


@pytest.mark.django_db
def test_non_usd_and_row_errors_are_shown_without_raw_invalid_values(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    secret_date = "PRIVATE-BAD-DATE"
    secret_amount = "PRIVATE-BAD-AMOUNT"
    content = f"When,Memo,Amount,Currency\n{secret_date},SYNTHETIC ITEM,{secret_amount},EUR\n".encode()
    staged = upload(client, account, content)
    token = staged.context["mapping_form"].initial["token"]

    response = client.post(reverse("csv-import-preview", args=(account.pk,)), mapping_data(token))

    assert response.context["preview"].invalid_count == 1
    assert b"Currency must be USD" in response.content
    assert secret_date.encode() not in response.content
    assert secret_amount.encode() not in response.content


@pytest.mark.django_db
def test_source_rows_are_not_logged(caplog, staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    secret = "PRIVATE-SOURCE-ROW-SECRET"
    content = f"When,Memo,Amount\ninvalid,{secret},invalid\n".encode()

    with caplog.at_level(logging.DEBUG):
        staged = upload(client, account, content)
        token = staged.context["mapping_form"].initial["token"]
        client.post(
            reverse("csv-import-preview", args=(account.pk,)),
            mapping_data(token, currency_column=""),
        )
    assert secret not in caplog.text


@pytest.mark.django_db
def test_stage_is_bound_to_user_and_account(staging_settings):
    owner_user, owner = make_person("owner")
    other_user, other = make_person("other")
    owner_account = Account.objects.create(name="Owner", account_type="checking", owner=owner)
    other_account = Account.objects.create(name="Other", account_type="checking", owner=other)
    owner_client = Client()
    owner_client.force_login(owner_user)
    token = upload(owner_client, owner_account).context["mapping_form"].initial["token"]

    other_client = Client()
    other_client.force_login(other_user)
    response = other_client.post(
        reverse("csv-import-preview", args=(other_account.pk,)), mapping_data(token)
    )
    assert response.status_code == 404

    # Even within the uploader's session, a token cannot move to another visible account.
    second = Account.objects.create(name="Second", account_type="checking", owner=owner)
    response = owner_client.post(reverse("csv-import-preview", args=(second.pk,)), mapping_data(token))
    assert response.status_code == 404


@pytest.mark.django_db
def test_cancel_and_expiry_delete_staged_content(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    token = upload(client, account).context["mapping_form"].initial["token"]
    path = Path(staging_settings) / f"{token}.csvstage"

    cancelled = client.post(
        reverse("csv-import-preview", args=(account.pk,)), {"action": "cancel", "token": token}
    )
    assert cancelled.status_code == 302
    assert not path.exists()

    token = upload(client, account).context["mapping_form"].initial["token"]
    path = Path(staging_settings) / f"{token}.csvstage"
    with patch("finance.csv_import.staging.time.time", return_value=time.time() + 4000):
        expired = client.post(reverse("csv-import-preview", args=(account.pk,)), mapping_data(token))
    assert expired.status_code == 404
    assert not path.exists()


@pytest.mark.django_db
def test_bad_uploads_and_size_cap_are_safe_and_not_staged(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)

    malformed = upload(client, account, b"When,Memo\n\"unterminated")
    assert malformed.status_code == 200
    assert b"malformed quoting" in malformed.content
    assert not list(Path(staging_settings).glob("*.csvstage"))

    oversized = upload(client, account, b"x" * (MAX_FILE_BYTES + 1))
    assert oversized.status_code == 200
    assert b"exceeds the 5 MB limit" in oversized.content
    assert not list(Path(staging_settings).glob("*.csvstage"))


def _spooled_temp_files():
    """A spy on Django's upload temp-file creation, which is what would write to disk."""
    from django.core.files import uploadedfile

    return patch.object(uploadedfile.tempfile, "NamedTemporaryFile", wraps=uploadedfile.tempfile.NamedTemporaryFile)


@pytest.mark.django_db
def test_a_normal_sized_upload_is_never_spooled_to_a_temp_file_on_disk(staging_settings):
    # Django writes uploads over FILE_UPLOAD_MAX_MEMORY_SIZE (2.5 MB by default)
    # to a temporary file in /tmp before our code sees them, which would put a
    # real bank export on disk even though staging itself is memory-backed.
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    row = b"09/27/2026," + b"S" * 1000 + b",-12.34,USD\n"
    content = b"When,Memo,Amount,Currency\n" + row * 3000

    with _spooled_temp_files() as spool:
        response = upload(client, account, content)

    assert len(content) > 2_621_440
    assert spool.call_count == 0
    assert response.status_code == 200
    assert len(list(Path(staging_settings).glob("*.csvstage"))) == 1


@pytest.mark.django_db
def test_a_very_large_upload_is_refused_without_touching_disk(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)

    with _spooled_temp_files() as spool:
        response = upload(client, account, b"x" * (12 * 1024 * 1024))

    assert spool.call_count == 0
    assert response.status_code == 200
    assert b"at most 5 MB" in response.content
    assert not list(Path(staging_settings).glob("*.csvstage"))


@pytest.mark.django_db
def test_uploading_a_csv_does_not_extend_the_session(staging_settings):
    # Staging writes to the session, and Django's default expiry slides with every
    # save, so repeated uploads used to keep a session alive past its fixed 28 days.
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
    session_key = client.session.session_key
    at_sign_in = Session.objects.get(session_key=session_key).expire_date

    time.sleep(0.05)
    response = upload(client, account)

    assert response.status_code == 200
    assert Session.objects.get(session_key=session_key).expire_date == at_sign_in


@pytest.mark.django_db
def test_get_after_upload_restores_mapping_and_cancel_from_live_stage(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    other = Account.objects.create(name="Synthetic Savings", account_type="savings", owner=person)
    client = Client()
    client.force_login(user)
    staged = upload(client, account)
    token = staged.context["mapping_form"].initial["token"]
    preview_url = reverse("csv-import-preview", args=(account.pk,))

    refreshed = client.get(preview_url)
    other_get = client.get(reverse("csv-import-preview", args=(other.pk,)))

    assert refreshed.status_code == 200
    assert refreshed.context["mapping_form"].initial["token"] == token
    assert list(refreshed.context["headers"]) == ["When", "Memo", "Amount", "Currency"]
    assert b"Cancel and delete upload" in refreshed.content
    assert other_get.status_code == 200
    assert other_get.context.get("mapping_form") is None
    assert b"Cancel and delete upload" not in other_get.content

    cancelled = client.post(preview_url, {"action": "cancel", "token": token})
    assert cancelled.status_code == 302
    assert not (Path(staging_settings) / f"{token}.csvstage").exists()
    blank = client.get(preview_url)
    assert blank.context.get("mapping_form") is None
    assert b"Cancel and delete upload" not in blank.content


@pytest.mark.django_db
def test_reupload_replaces_prior_stage_for_the_same_account(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    other = Account.objects.create(name="Synthetic Savings", account_type="savings", owner=person)
    client = Client()
    client.force_login(user)
    first_token = upload(client, account).context["mapping_form"].initial["token"]
    other_token = upload(client, other).context["mapping_form"].initial["token"]
    second_csv = b"When,Memo,Amount,Currency\n09/28/2026,SYNTHETIC CAFE,-5.00,USD\n"
    second_token = upload(client, account, second_csv).context["mapping_form"].initial["token"]

    files = {path.name for path in Path(staging_settings).glob("*.csvstage")}
    stages = client.session[SESSION_KEY]
    assert first_token != second_token
    assert files == {f"{second_token}.csvstage", f"{other_token}.csvstage"}
    assert set(stages) == {second_token, other_token}
    assert stages[second_token]["account_id"] == account.pk
    assert stages[other_token]["account_id"] == other.pk
    assert not (Path(staging_settings) / f"{first_token}.csvstage").exists()
    assert (Path(staging_settings) / f"{second_token}.csvstage").read_bytes() == second_csv


@pytest.mark.django_db
def test_get_does_not_restore_an_expired_stage(staging_settings):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    token = upload(client, account).context["mapping_form"].initial["token"]
    path = Path(staging_settings) / f"{token}.csvstage"

    with patch("finance.csv_import.staging.time.time", return_value=time.time() + 4000):
        expired = client.get(reverse("csv-import-preview", args=(account.pk,)))

    assert expired.status_code == 200
    assert expired.context.get("mapping_form") is None
    assert b"Cancel and delete upload" not in expired.content
    assert not path.exists()
