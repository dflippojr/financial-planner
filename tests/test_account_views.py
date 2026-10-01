from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.lifecycle_services import archive_account, share_account
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None, share_mode=None):
    if share_mode is None and scope == Account.Scope.HOUSEHOLD:
        share_mode = Account.ShareMode.CO_OWNED
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def missing_id(account):
    return account.pk + 999


def assert_same_404(response, missing_response):
    assert response.status_code == missing_response.status_code == 404
    assert response.content == missing_response.content


@pytest.mark.django_db
def test_anonymous_accounts_page_redirects():
    response = Client().get(reverse("account-list"))
    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_empty_accounts_page_prompts_to_add_first_account():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    response = client.get(reverse("account-list"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Add your first account to import transactions" in content
    assert "Co-owned (household)" in content
    assert "Lent (household)" in content
    assert "Currency" not in content
    accounts_item = next(item for item in response.context["nav_items"] if item["label"] == "Accounts")
    import_item = next(item for item in response.context["nav_items"] if item["label"] == "Import")
    assert accounts_item["url"] == import_item["url"] == reverse("account-list")
    assert accounts_item["active"]
    assert not import_item["active"]


@pytest.mark.django_db
def test_add_private_account_redirects_to_import():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)

    response = client.post(
        reverse("account-list"),
        {"name": "Synthetic Private", "account_type": Account.Type.CHECKING, "sharing": Account.Scope.PRIVATE},
    )
    account = Account.objects.get(name="Synthetic Private")

    assert response.status_code == 302
    assert response.url == reverse("csv-import-preview", args=(account.pk,))
    assert account.owner_id == owner.pk
    assert account.scope == Account.Scope.PRIVATE
    assert account.household_id is None
    assert account.currency == "USD"


@pytest.mark.django_db
def test_add_household_account_redirects_to_import():
    owner = make_person("owner")
    household = make_household(owner)
    client = signed_in(owner)

    response = client.post(
        reverse("account-list"),
        {"name": "Synthetic Shared", "account_type": Account.Type.SAVINGS, "sharing": Account.ShareMode.CO_OWNED},
    )
    account = Account.objects.get(name="Synthetic Shared")

    assert response.status_code == 302
    assert response.url == reverse("csv-import-preview", args=(account.pk,))
    assert account.scope == Account.Scope.HOUSEHOLD
    assert account.share_mode == Account.ShareMode.CO_OWNED
    assert account.household_id == household.pk
    assert account.account_type == Account.Type.SAVINGS


@pytest.mark.django_db
def test_member_without_household_cannot_create_shared_account():
    owner = make_person("solo")
    client = signed_in(owner)
    page = client.get(reverse("account-list"))
    response = client.post(
        reverse("account-list"),
        {"name": "Forged Shared", "account_type": Account.Type.CHECKING, "sharing": Account.Scope.HOUSEHOLD},
    )

    assert "Shared with household" not in page.content.decode()
    assert response.status_code == 200
    assert not Account.objects.filter(name="Forged Shared").exists()


@pytest.mark.django_db
def test_accounts_page_lists_visible_accounts_only():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    make_household(outsider, name="Other Household")
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Household Card", account_type=Account.Type.CREDIT_CARD, scope=Account.Scope.HOUSEHOLD, household=household)
    member_private = make_account(member, name="Member Private")
    secret = make_account(outsider, name="SECRET OTHER LEDGER")
    batch = ImportBatch.objects.create(
        account=private,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="c" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    Transaction.objects.create(
        account=private,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-500,
        currency="USD",
        description="Synthetic grocery",
        source_row_number=1,
        fingerprint="d" * 64,
        original_fields={"payee": "Store"},
    )

    owner_page = signed_in(owner).get(reverse("account-list")).content.decode()
    member_page = signed_in(member).get(reverse("account-list")).content.decode()
    outsider_page = signed_in(outsider).get(reverse("account-list")).content.decode()

    assert "Owner Private" in owner_page
    assert "Household Card" in owner_page
    assert "Member Private" not in owner_page
    assert "SECRET OTHER LEDGER" not in owner_page
    assert reverse("csv-import-preview", args=(private.pk,)) in owner_page
    assert timezone.localdate().isoformat() in owner_page
    assert "Share with household" in owner_page
    assert "Make private" in owner_page
    assert "Household Card" in member_page
    assert "Owner Private" not in member_page
    assert "Member Private" in member_page
    assert "SECRET OTHER LEDGER" not in member_page
    assert "SECRET OTHER LEDGER" in outsider_page
    assert "Household Card" not in outsider_page
    assert secret.name not in member_page


@pytest.mark.django_db
def test_owner_and_household_member_can_rename_shared_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    account = make_account(owner, name="Old Name", scope=Account.Scope.HOUSEHOLD, household=household)

    owner_response = signed_in(owner).post(reverse("account-rename", args=(account.pk,)), {"name": "Owner Rename"})
    account.refresh_from_db()
    member_response = signed_in(member).post(reverse("account-rename", args=(account.pk,)), {"name": "Member Rename"})
    account.refresh_from_db()

    assert owner_response.status_code == member_response.status_code == 302
    assert account.name == "Member Rename"


@pytest.mark.django_db
def test_rename_rejects_empty_and_too_long_names():
    owner = make_person("owner")
    account = make_account(owner, name="Keep Name")
    client = signed_in(owner)
    empty = client.post(reverse("account-rename", args=(account.pk,)), {"name": "   "})
    too_long = client.post(reverse("account-rename", args=(account.pk,)), {"name": "x" * 151})
    account.refresh_from_db()

    assert empty.status_code == too_long.status_code == 302
    assert account.name == "Keep Name"


@pytest.mark.parametrize(
    ("action", "url_name"),
    (
        ("rename", "account-rename"),
        ("share", "account-share"),
        ("share-mode", "account-share-mode"),
        ("unshare", "account-unshare"),
        ("archive", "account-archive"),
        ("delete", "account-delete"),
    ),
)
@pytest.mark.django_db
def test_unauthorized_account_actions_return_identical_404(action, url_name):
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    payloads = {
        "rename": {"name": "Hacked"},
        "delete": {"confirm_name": "Shared"},
        "share": {"share_mode": Account.ShareMode.CO_OWNED},
        "share-mode": {"share_mode": Account.ShareMode.LENT},
    }
    payload = payloads.get(action, {})

    member_private = signed_in(member).post(reverse(url_name, args=(private.pk,)), payload)
    outsider_private = signed_in(outsider).post(reverse(url_name, args=(private.pk,)), payload)
    outsider_shared = signed_in(outsider).post(reverse(url_name, args=(shared.pk,)), payload)
    missing = signed_in(owner).post(reverse(url_name, args=(missing_id(private),)), payload)

    assert_same_404(member_private, missing)
    assert_same_404(outsider_private, missing)
    assert_same_404(outsider_shared, missing)
    private.refresh_from_db()
    shared.refresh_from_db()
    assert private.name == "Owner Private"
    assert private.scope == Account.Scope.PRIVATE
    assert private.status == Account.Status.ACTIVE
    assert shared.scope == Account.Scope.HOUSEHOLD
    assert shared.status == Account.Status.ACTIVE


@pytest.mark.django_db
def test_share_unshare_and_archive_use_lifecycle_rules():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="To Share")
    already_shared = make_account(owner, name="Already Shared", scope=Account.Scope.HOUSEHOLD, household=household)

    share = signed_in(owner).post(
        reverse("account-share", args=(private.pk,)),
        {"share_mode": Account.ShareMode.CO_OWNED},
    )
    private.refresh_from_db()
    assert share.status_code == 302
    assert private.scope == Account.Scope.HOUSEHOLD
    assert private.household_id == household.pk
    assert private.share_mode == Account.ShareMode.CO_OWNED

    member_unshare = signed_in(member).post(reverse("account-unshare", args=(private.pk,)))
    private.refresh_from_db()
    member_share_others = signed_in(member).post(
        reverse("account-share", args=(already_shared.pk,)),
        {"share_mode": Account.ShareMode.CO_OWNED},
    )
    archive = signed_in(member).post(reverse("account-archive", args=(already_shared.pk,)))
    already_shared.refresh_from_db()
    missing = signed_in(owner).post(
        reverse("account-share", args=(missing_id(private),)),
        {"share_mode": Account.ShareMode.CO_OWNED},
    )

    assert member_unshare.status_code == 302
    assert private.scope == Account.Scope.PRIVATE
    assert_same_404(member_share_others, missing)
    assert archive.status_code == 302
    assert already_shared.status == Account.Status.ARCHIVED
    assert already_shared.archived_at is not None


@pytest.mark.django_db
def test_archived_accounts_have_no_row_actions_and_reject_posts():
    owner = make_person("owner")
    account = make_account(owner, name="Old Ledger")
    archive_account(owner.user, account.pk)
    client = signed_in(owner)
    page = client.get(reverse("account-list")).content.decode()
    rename = client.post(reverse("account-rename", args=(account.pk,)), {"name": "New"})
    missing = client.post(reverse("account-rename", args=(missing_id(account),)), {"name": "New"})

    assert "Old Ledger" in page
    assert reverse("csv-import-preview", args=(account.pk,)) not in page
    assert reverse("account-delete", args=(account.pk,)) in page
    assert_same_404(rename, missing)
    account.refresh_from_db()
    assert account.name == "Old Ledger"


@pytest.mark.django_db
def test_get_mutations_are_rejected():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    client = signed_in(owner)

    assert client.get(reverse("account-rename", args=(account.pk,))).status_code == 405
    assert client.get(reverse("account-share", args=(account.pk,))).status_code == 405
    assert client.get(reverse("account-share-mode", args=(account.pk,))).status_code == 405
    assert client.get(reverse("account-unshare", args=(account.pk,))).status_code == 405
    assert client.get(reverse("account-archive", args=(account.pk,))).status_code == 405
    assert account.status == Account.Status.ACTIVE


@pytest.mark.django_db
def test_import_nav_points_to_accounts_when_an_account_exists():
    owner = make_person("owner")
    make_account(owner)
    home = signed_in(owner).get(reverse("home"))
    import_item = next(item for item in home.context["nav_items"] if item["label"] == "Import")
    assert import_item["url"] == reverse("account-list")
    assert reverse("home") != import_item["url"]


@pytest.mark.django_db
def test_share_is_owner_only_even_for_household_member():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner)
    share_account(owner.user, private.pk, Account.ShareMode.CO_OWNED)
    unshare = signed_in(owner).post(reverse("account-unshare", args=(private.pk,)))
    private.refresh_from_db()
    member_share = signed_in(member).post(
        reverse("account-share", args=(private.pk,)),
        {"share_mode": Account.ShareMode.CO_OWNED},
    )
    missing = signed_in(owner).post(
        reverse("account-share", args=(missing_id(private),)),
        {"share_mode": Account.ShareMode.CO_OWNED},
    )

    assert unshare.status_code == 302
    assert private.scope == Account.Scope.PRIVATE
    assert_same_404(member_share, missing)
    private.refresh_from_db()
    assert private.scope == Account.Scope.PRIVATE


@pytest.mark.django_db
def test_rename_is_refused_when_the_account_is_unshared_mid_request(monkeypatch):
    import finance.lifecycle_services as lifecycle

    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    account = make_account(owner, name="Synthetic Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    real_lock = lifecycle.lock_actor_household

    def unshare_first(person):
        # The owner makes the account private after the member's lookup and
        # before the member's rename takes its locks.
        Account.objects.filter(pk=account.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode=None)
        return real_lock(person)

    monkeypatch.setattr(lifecycle, "lock_actor_household", unshare_first)
    client = signed_in(member)

    response = client.post(reverse("account-rename", args=(account.pk,)), {"name": "Synthetic Renamed"})

    account.refresh_from_db()
    assert response.status_code == 404
    assert account.name == "Synthetic Shared"


@pytest.mark.django_db
def test_lent_share_controls_and_mode_switch_on_accounts_page():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="To Lent")
    lent = make_account(
        owner,
        name="Already Lent",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )

    share = signed_in(owner).post(
        reverse("account-share", args=(private.pk,)),
        {"share_mode": Account.ShareMode.LENT},
    )
    private.refresh_from_db()
    owner_page = signed_in(owner).get(reverse("account-list")).content.decode()
    member_page = signed_in(member).get(reverse("account-list")).content.decode()

    assert share.status_code == 302
    assert private.share_mode == Account.ShareMode.LENT
    assert "Lent" in owner_page
    assert "Make co-owned" in owner_page
    assert reverse("account-unshare", args=(lent.pk,)) in owner_page
    assert reverse("account-archive", args=(lent.pk,)) in owner_page
    assert reverse("account-unshare", args=(lent.pk,)) not in member_page
    assert reverse("account-archive", args=(lent.pk,)) not in member_page
    assert "Make co-owned" not in member_page
    assert "Make lent" not in member_page

    denied_unshare = signed_in(member).post(reverse("account-unshare", args=(lent.pk,)))
    denied_archive = signed_in(member).post(reverse("account-archive", args=(lent.pk,)))
    denied_mode = signed_in(member).post(
        reverse("account-share-mode", args=(lent.pk,)),
        {"share_mode": Account.ShareMode.CO_OWNED, "confirm_give_up_ownership": "on"},
    )
    missing = signed_in(owner).post(reverse("account-unshare", args=(missing_id(lent),)))
    assert_same_404(denied_unshare, missing)
    assert_same_404(denied_archive, missing)
    assert_same_404(denied_mode, missing)

    unconfirmed = signed_in(owner).post(
        reverse("account-share-mode", args=(lent.pk,)),
        {"share_mode": Account.ShareMode.CO_OWNED},
    )
    lent.refresh_from_db()
    assert_same_404(unconfirmed, missing)
    assert lent.share_mode == Account.ShareMode.LENT

    confirmed = signed_in(owner).post(
        reverse("account-share-mode", args=(lent.pk,)),
        {"share_mode": Account.ShareMode.CO_OWNED, "confirm_give_up_ownership": "on"},
    )
    lent.refresh_from_db()
    assert confirmed.status_code == 302
    assert lent.share_mode == Account.ShareMode.CO_OWNED

    to_lent = signed_in(owner).post(
        reverse("account-share-mode", args=(lent.pk,)),
        {"share_mode": Account.ShareMode.LENT},
    )
    lent.refresh_from_db()
    assert to_lent.status_code == 302
    assert lent.share_mode == Account.ShareMode.LENT
