"""Each member can dismiss the investment-activity notice (#699).

The notice shows on Home, Spending and each spending-category page when an
investment account is in scope. Dismissal is stored on the member's Person,
so it holds on every device and never hides the notice for another member,
and it resets when the notice's wording changes.
"""
import html
import re

import pytest
from django.test import Client
from django.urls import reverse

from finance import cash_flow
from finance.models import Account, Person
from finance.notice_dismissal import notice_hash
from tests.a11y import assert_accessible
from tests.test_net_worth import make_account, make_household, make_person, signed_in

NOTICE = cash_flow.INVESTMENT_NOTICE
DISMISS_LABEL = 'aria-label="Dismiss the investment activity notice"'
SPENDING_QUERY = "?date_from=2026-01-01&date_to=2026-03-31&grouping=month&tab=trends"


def _pages():
    return (
        reverse("home"),
        reverse("spending-by-category"),
        reverse("spending-category-uncategorized"),
    )


@pytest.fixture
def household():
    """Two members sharing a household brokerage account, so both see the notice."""
    owner = make_person("owner")
    member = make_person("member")
    home = make_household(owner, member)
    make_account(
        owner,
        name="Synthetic Brokerage",
        account_type=Account.Type.INVESTMENT,
        scope=Account.Scope.HOUSEHOLD,
        household=home,
    )
    return owner, member


def _notice(body):
    """The rendered notice block, or None when the page has none."""
    match = re.search(r'<div role="note"[^>]*data-investment-notice[^>]*>.*?</div>', body, re.S)
    return match.group(0) if match else None


def _dismiss(client, *, shown=None, next_url="/", json=False):
    headers = {"HTTP_ACCEPT": "application/json"} if json else {}
    return client.post(
        reverse("investment-notice-dismiss"),
        {"notice": notice_hash() if shown is None else shown, "next": next_url},
        **headers,
    )


@pytest.mark.django_db
def test_each_page_renders_the_notice_with_an_accessible_dismiss_form(household):
    owner, _member = household
    client = signed_in(owner)
    for page in _pages():
        body = client.get(page + SPENDING_QUERY).content.decode()
        notice = _notice(body)
        assert notice is not None, page
        assert NOTICE in notice
        assert f'action="{reverse("investment-notice-dismiss")}"' in notice
        assert 'name="csrfmiddlewaretoken"' in notice
        assert f'name="notice" value="{notice_hash()}"' in notice
        # The no-JS path returns to this exact page, query string included.
        assert f'name="next" value="{html.escape(page + SPENDING_QUERY)}"' in notice
        assert re.search(r'<button type="submit"[^>]*' + re.escape(DISMISS_LABEL), notice)
        # A 44 px target on phones (size-11), smaller beside the text from md up.
        assert "size-11 md:size-8" in notice
        assert "js/notice-dismiss.js" in body
        assert_accessible(body)


@pytest.mark.django_db
def test_dismissing_with_script_hides_it_for_that_member_on_every_page_and_device(household):
    owner, member = household
    reply = _dismiss(signed_in(owner), json=True)
    assert reply.status_code == 200
    assert reply.json() == {"dismissed": True}
    owner.refresh_from_db()
    assert owner.investment_notice_dismissed_hash == notice_hash()

    # A new session stands in for another device: the choice is on the server.
    other_device = signed_in(owner)
    for page in _pages():
        body = other_device.get(page).content.decode()
        assert _notice(body) is None, page
        assert DISMISS_LABEL not in body
        assert "js/notice-dismiss.js" not in body
        # The explanation stays reachable in the page's More note.
        assert "<summary>More</summary>" in body
        assert NOTICE in body, page

    # Another household member still sees it until they dismiss it themselves.
    member_client = signed_in(member)
    for page in _pages():
        assert _notice(member_client.get(page).content.decode()) is not None, page
    member.refresh_from_db()
    assert member.investment_notice_dismissed_hash == ""


@pytest.mark.django_db
def test_home_phone_more_note_names_the_exclusion(household):
    owner, _member = household
    client = signed_in(owner)
    _dismiss(client)
    body = client.get(reverse("home")).content.decode()
    phone_note = body.split('<details class="px-1 text-sm md:hidden">', 1)[1].split("</details>", 1)[0]
    assert "Investment activity left out." in phone_note
    assert NOTICE in phone_note


@pytest.mark.django_db
def test_dismissing_without_script_redirects_back_with_the_query_string(household):
    owner, _member = household
    client = signed_in(owner)
    target = reverse("spending-by-category") + SPENDING_QUERY
    reply = _dismiss(client, next_url=target)
    assert reply.status_code == 302
    assert reply["Location"] == target
    assert _notice(client.get(target).content.decode()) is None


@pytest.mark.django_db
@pytest.mark.parametrize("next_url", ["https://elsewhere.example/", "//elsewhere.example/spending/", ""])
def test_an_off_site_or_missing_next_returns_home(household, next_url):
    owner, _member = household
    reply = _dismiss(signed_in(owner), next_url=next_url)
    assert reply.status_code == 302
    assert reply["Location"] == reverse("home")
    owner.refresh_from_db()
    assert owner.investment_notice_dismissed_hash == notice_hash()


@pytest.mark.django_db
def test_new_wording_shows_the_notice_again_for_everyone_who_dismissed_the_old(household, monkeypatch):
    owner, member = household
    _dismiss(signed_in(owner))
    _dismiss(signed_in(member))
    for person in (owner, member):
        assert _notice(signed_in(person).get(reverse("home")).content.decode()) is None

    reworded = "Investment activity is still left out of income and spending (synthetic new wording)."
    monkeypatch.setattr(cash_flow, "INVESTMENT_NOTICE", reworded)
    for person in (owner, member):
        client = signed_in(person)
        for page in _pages():
            notice = _notice(client.get(page).content.decode())
            assert notice is not None and reworded in notice, page
            assert f'name="notice" value="{notice_hash(reworded)}"' in notice

    # Dismissing the new wording hides it again.
    assert _dismiss(signed_in(owner), shown=notice_hash(reworded), json=True).status_code == 200
    assert _notice(signed_in(owner).get(reverse("home")).content.decode()) is None


@pytest.mark.django_db
def test_a_page_rendered_before_the_wording_changed_cannot_dismiss_the_new_wording(household):
    owner, _member = household
    stale = notice_hash("An older synthetic wording.")
    client = signed_in(owner)
    reply = _dismiss(client, shown=stale, json=True)
    assert reply.status_code == 409
    assert reply.json()["dismissed"] is False
    target = reverse("home") + "?grouping=week"
    reply = _dismiss(client, shown=stale, next_url=target)
    assert reply.status_code == 302 and reply["Location"] == target
    owner.refresh_from_db()
    assert owner.investment_notice_dismissed_hash == ""
    assert _notice(client.get(reverse("home")).content.decode()) is not None


@pytest.mark.django_db
def test_dismissing_requires_a_csrf_token(household):
    owner, _member = household
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner.user)
    refused = client.post(reverse("investment-notice-dismiss"), {"notice": notice_hash(), "next": "/"})
    assert refused.status_code == 403
    owner.refresh_from_db()
    assert owner.investment_notice_dismissed_hash == ""

    body = client.get(reverse("home")).content.decode()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', _notice(body)).group(1)
    accepted = client.post(
        reverse("investment-notice-dismiss"),
        {"notice": notice_hash(), "next": "/", "csrfmiddlewaretoken": token},
    )
    assert accepted.status_code == 302
    owner.refresh_from_db()
    assert owner.investment_notice_dismissed_hash == notice_hash()


@pytest.mark.django_db
def test_dismiss_is_post_only_and_needs_a_signed_in_member(household):
    owner, _member = household
    assert signed_in(owner).get(reverse("investment-notice-dismiss")).status_code == 405
    anonymous = Client().post(reverse("investment-notice-dismiss"), {"notice": notice_hash()})
    assert anonymous.status_code == 302
    assert reverse("investment-notice-dismiss") not in anonymous["Location"].split("?")[0]
    assert not Person.objects.exclude(investment_notice_dismissed_hash="").exists()


@pytest.mark.django_db
def test_no_investment_account_means_no_notice_and_no_more_note():
    owner = make_person("owner")
    make_household(owner)
    make_account(owner, name="Synthetic Checking")
    client = signed_in(owner)
    for page in _pages():
        body = client.get(page).content.decode()
        assert _notice(body) is None, page
        assert NOTICE not in body, page

