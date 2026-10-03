import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.csv_import.parser import Mapping
from finance.csv_import.saved_mappings import (
    HEADERS_DO_NOT_MATCH,
    LOCKED_PARSING_MESSAGE,
    save_csv_mapping,
    update_csv_mapping,
)
from finance.csv_import.staging import SESSION_KEY
from finance.models import Account, Household, ImportBatch, Membership, Person, SavedCsvMapping, Transaction


PASSWORD = "Synthetic-passphrase-42!"
CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"
MISMATCH_CSV = b"Posted,Payee,Total\n09/27/2026,SYNTHETIC GROCER,-12.34\n"

HAND_MAPPING = Mapping(
    date_column="When",
    description_column="Memo",
    date_format="mdy_slash_4",
    number_format="dot_comma",
    amount_mode="signed",
    amount_column="Amount",
    currency_column="Currency",
)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def household_for(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


@pytest.fixture
def staging_settings(tmp_path):
    with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path, CSV_IMPORT_STAGE_TTL_SECONDS=3600):
        yield tmp_path


def mapping_post(token, **changes):
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
        "save_mapping_as": "",
    }
    data.update(changes)
    return data


@pytest.mark.django_db
def test_save_then_reuse_matches_hand_preview_and_mismatch_falls_back(staging_settings):
    user, person = make_person("owner")
    household_for(person)
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))

    token = client.post(
        preview_url,
        {"action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv")},
    ).context["mapping_form"].initial["token"]
    hand = client.post(preview_url, mapping_post(token))
    saved_response = client.post(
        preview_url,
        mapping_post(token, action="save_mapping", save_mapping_as="Synthetic store"),
    )
    assert saved_response.status_code == 200
    assert b"The mapping was saved" in saved_response.content
    mapping = SavedCsvMapping.objects.get(name="Synthetic store")
    assert mapping.headers == ["When", "Memo", "Amount", "Currency"]
    assert mapping.amount_column == "Amount"

    reused = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{mapping.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )
    assert reused.status_code == 200
    assert reused.context["preview"].rows[0].amount_minor == hand.context["preview"].rows[0].amount_minor
    assert reused.context["preview"].rows[0].description == hand.context["preview"].rows[0].description
    assert reused.context["saved_mapping"] == mapping

    mismatched = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{mapping.pk}",
            "csv_file": SimpleUploadedFile("other.csv", MISMATCH_CSV, "text/csv"),
        },
    )
    assert mismatched.status_code == 200
    assert mismatched.context["header_mismatch_message"] == HEADERS_DO_NOT_MATCH
    assert mismatched.context.get("saved_mapping") is None
    assert mismatched.context.get("preview") is None
    assert b"Map the uploaded columns" in mismatched.content


@pytest.mark.django_db
def test_commit_locks_parsing_fields_but_name_and_default_stay_editable(staging_settings):
    user, person = make_person("owner")
    household_for(person)
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    token = client.post(
        preview_url,
        {"action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv")},
    ).context["mapping_form"].initial["token"]
    client.post(preview_url, mapping_post(token, action="save_mapping", save_mapping_as="Locked store"))
    mapping = SavedCsvMapping.objects.get(name="Locked store")
    token = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{mapping.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    ).context["mapping_form"]["token"].value()
    imported = client.post(
        preview_url,
        {
            "action": "commit",
            "token": token,
            "source": "huntington",
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
        follow=True,
    )
    assert imported.status_code == 200
    mapping.refresh_from_db()
    assert mapping.locked_at is not None
    assert ImportBatch.objects.get().saved_csv_mapping_id == mapping.pk
    assert Transaction.objects.count() == 1

    with pytest.raises(ValidationError, match="locked after an import"):
        update_csv_mapping(
            user,
            mapping.pk,
            name="Locked store",
            mapping=Mapping(
                date_column="When",
                description_column="Memo",
                date_format="mdy_slash_4",
                number_format="dot_comma",
                amount_mode="signed",
                amount_column="Currency",
                invert_sign=True,
                currency_column="Currency",
            ),
        )
    mapping.refresh_from_db()
    assert mapping.amount_column == "Amount"
    assert mapping.invert_sign is False

    updated = update_csv_mapping(
        user,
        mapping.pk,
        name="Renamed store",
        default_account_ids=[account.pk],
    )
    assert updated.name == "Renamed store"
    account.refresh_from_db()
    assert account.default_saved_csv_mapping_id == mapping.pk

    edit = client.post(
        reverse("csv-mapping-edit", args=(mapping.pk,)),
        {
            "name": "Renamed store",
            "date_column": "Memo",
            "description_column": "When",
            "date_format": "iso",
            "number_format": "dot_none",
            "amount_mode": "signed",
            "amount_column": "Memo",
            "invert_sign": "on",
            "default_accounts": [str(account.pk)],
        },
        follow=True,
    )
    assert edit.status_code == 200
    mapping.refresh_from_db()
    assert mapping.amount_column == "Amount"
    assert mapping.invert_sign is False
    assert mapping.date_format == "mdy_slash_4"
    assert LOCKED_PARSING_MESSAGE.encode() in client.get(reverse("csv-mapping-edit", args=(mapping.pk,))).content


@pytest.mark.django_db
def test_account_default_is_preselected_on_import_page(staging_settings):
    user, person = make_person("owner")
    household_for(person)
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    mapping = save_csv_mapping(
        user,
        name="Default store",
        headers=("When", "Memo", "Amount", "Currency"),
        mapping=HAND_MAPPING,
        account=account,
        set_as_account_default=True,
    )
    client = Client()
    client.force_login(user)
    page = client.get(reverse("csv-import-preview", args=(account.pk,)))
    assert page.context["upload_form"].initial["import_profile"] == f"saved:{mapping.pk}"
    html = page.content.decode()
    assert f'value="saved:{mapping.pk}"' in html
    assert "selected" in html


@pytest.mark.django_db
def test_saved_mapping_hidden_from_other_household_and_former_member(staging_settings):
    owner_user, owner = make_person("owner")
    member_user, member = make_person("member")
    stranger_user, stranger = make_person("stranger")
    household = household_for(owner, member)
    household_for(stranger)
    account = Account.objects.create(
        name="Shared",
        account_type="checking",
        owner=owner,
        scope="household",
        household=household,
        share_mode="co_owned",
    )
    mapping = save_csv_mapping(
        owner_user,
        name="Household store",
        headers=("When", "Memo", "Amount", "Currency"),
        mapping=HAND_MAPPING,
    )
    member_client = Client()
    member_client.force_login(member_user)
    listed = member_client.get(reverse("csv-mapping-list"))
    assert b"Household store" in listed.content
    assert member_client.get(reverse("csv-mapping-edit", args=(mapping.pk,))).status_code == 200

    stranger_client = Client()
    stranger_client.force_login(stranger_user)
    stranger_list = stranger_client.get(reverse("csv-mapping-list"))
    assert b"Household store" not in stranger_list.content
    assert stranger_client.get(reverse("csv-mapping-edit", args=(mapping.pk,))).status_code == 404
    stranger_account = Account.objects.create(name="Other checking", account_type="checking", owner=stranger)
    stranger_upload = stranger_client.post(
        reverse("csv-import-preview", args=(stranger_account.pk,)),
        {
            "action": "upload",
            "import_profile": f"saved:{mapping.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )
    assert stranger_upload.status_code == 200
    assert stranger_upload.context.get("saved_mapping") is None

    Membership.objects.filter(person=member, household=household).update(ended_at=timezone.now())
    former = member_client.get(reverse("csv-mapping-list"))
    assert b"Household store" not in former.content
    assert member_client.get(reverse("csv-mapping-edit", args=(mapping.pk,))).status_code == 404
    former_preview = member_client.get(reverse("csv-import-preview", args=(account.pk,)))
    assert former_preview.status_code == 404


@pytest.mark.django_db
def test_used_mapping_is_archived_instead_of_deleted(staging_settings):
    user, person = make_person("owner")
    household_for(person)
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    token = client.post(
        preview_url,
        {"action": "upload", "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv")},
    ).context["mapping_form"].initial["token"]
    client.post(preview_url, mapping_post(token, action="save_mapping", save_mapping_as="Keep store"))
    mapping = SavedCsvMapping.objects.get()
    token = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{mapping.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    ).context["mapping_form"]["token"].value()
    client.post(
        preview_url,
        {
            "action": "commit",
            "token": token,
            "source": "huntington",
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
    )
    unused = save_csv_mapping(
        user,
        name="Unused store",
        headers=("When", "Memo", "Amount", "Currency"),
        mapping=HAND_MAPPING,
    )
    client.post(reverse("csv-mapping-list"), {"action": "delete", "mapping_id": str(unused.pk)})
    assert not SavedCsvMapping.objects.filter(pk=unused.pk).exists()
    client.post(reverse("csv-mapping-list"), {"action": "delete", "mapping_id": str(mapping.pk)})
    mapping.refresh_from_db()
    assert mapping.status == SavedCsvMapping.Status.ARCHIVED
    assert mapping.archived_at is not None


def _stage_profile(client, token):
    return client.session[SESSION_KEY][token]["import_profile"]


@pytest.mark.django_db
def test_restore_and_preview_fall_back_when_staged_saved_mapping_is_gone(staging_settings):
    user, person = make_person("owner")
    household_for(person)
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    mapping = save_csv_mapping(
        user,
        name="Soon deleted",
        headers=("When", "Memo", "Amount", "Currency"),
        mapping=HAND_MAPPING,
    )
    uploaded = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{mapping.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )
    token = uploaded.context["mapping_form"]["token"].value()
    assert _stage_profile(client, token) == f"saved:{mapping.pk}"

    client.post(reverse("csv-mapping-list"), {"action": "delete", "mapping_id": str(mapping.pk)})
    assert not SavedCsvMapping.objects.filter(pk=mapping.pk).exists()

    restored = client.get(preview_url)
    assert restored.status_code == 200
    assert restored.context["import_profile"] == "generic"
    assert restored.context.get("saved_mapping") is None
    assert b"Map the uploaded columns" in restored.content
    assert _stage_profile(client, token) == "generic"

    previewed = client.post(preview_url, mapping_post(token))
    assert previewed.status_code == 200
    assert previewed.context["preview"].valid_count == 1
    assert previewed.context["commit_available"] is True
    assert b"Save mapping" in previewed.content

    saved_again = client.post(
        preview_url,
        mapping_post(token, action="save_mapping", save_mapping_as="Generic after delete"),
    )
    assert saved_again.status_code == 200
    assert SavedCsvMapping.objects.filter(name="Generic after delete").exists()

    imported = client.post(
        preview_url,
        mapping_post(
            token,
            action="commit",
            source="huntington",
            date_range_start="2026-09-01",
            date_range_end="2026-09-30",
        ),
        follow=True,
    )
    assert imported.status_code == 200
    assert Transaction.objects.count() == 1


@pytest.mark.django_db
def test_restore_and_preview_fall_back_when_staged_saved_mapping_is_archived(staging_settings):
    user, person = make_person("owner")
    household_for(person)
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    client = Client()
    client.force_login(user)
    preview_url = reverse("csv-import-preview", args=(account.pk,))
    used = save_csv_mapping(
        user,
        name="Used then archived",
        headers=("When", "Memo", "Amount", "Currency"),
        mapping=HAND_MAPPING,
    )
    first_token = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{used.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    ).context["mapping_form"]["token"].value()
    client.post(
        preview_url,
        {
            "action": "commit",
            "token": first_token,
            "source": "huntington",
            "date_range_start": "2026-09-01",
            "date_range_end": "2026-09-30",
        },
    )
    staged = client.post(
        preview_url,
        {
            "action": "upload",
            "import_profile": f"saved:{used.pk}",
            "csv_file": SimpleUploadedFile("synthetic.csv", CSV, "text/csv"),
        },
    )
    token = staged.context["mapping_form"]["token"].value()
    client.post(reverse("csv-mapping-list"), {"action": "delete", "mapping_id": str(used.pk)})
    used.refresh_from_db()
    assert used.status == SavedCsvMapping.Status.ARCHIVED

    restored = client.get(preview_url)
    assert restored.context["import_profile"] == "generic"
    assert restored.context.get("saved_mapping") is None
    assert _stage_profile(client, token) == "generic"

    previewed = client.post(preview_url, mapping_post(token))
    assert previewed.context["preview"].valid_count == 1
    imported = client.post(
        preview_url,
        mapping_post(
            token,
            action="commit",
            source="huntington",
            date_range_start="2026-09-01",
            date_range_end="2026-09-30",
        ),
        follow=True,
    )
    assert imported.status_code == 200
    assert Transaction.objects.count() == 1
