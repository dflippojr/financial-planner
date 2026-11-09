"""The privacy sweeps' canary amounts can't collide with an id, and a real amount still trips them (#709)."""
import pytest
from django.http import HttpResponse
from django.urls import reverse

from finance.export import write_export_zip
from tests.privacy_world import FORBIDDEN, UNCATEGORIZED_MINOR, build_report_world, money_forms, signed_in
from tests.test_report_privacy_sweep import _fetch, _sweep_urls
from tests.test_route_access_sweep import OUTSIDER_FORBIDDEN, _client, _leaks

# A full PostgreSQL run takes primary-key sequences to about 15,000; a canary's bare
# minor string must stay far above anything an id can reach.
ID_CEILING = 100_000
MINORS = sorted({int(needle) for needle in (*FORBIDDEN, *OUTSIDER_FORBIDDEN) if needle.isdigit()})


def test_every_canary_minor_value_is_too_large_to_be_an_id():
    small = [minor for minor in MINORS if minor <= ID_CEILING]
    assert not small, f"canary minor values an id can reach: {small}"


def test_every_canary_is_searched_in_all_three_forms():
    for minor in MINORS:
        assert set(money_forms(minor)) <= set(OUTSIDER_FORBIDDEN)


def test_the_owner_sees_every_report_canary_amount_in_a_searched_form(db):
    """The forms match how the app renders these amounts, so a leaked one would be found."""
    world = build_report_world()
    client = signed_in(world.owner)
    pages = "\n".join(_fetch(client, url, params) for url, params in _sweep_urls(client, world))
    unseen = [minor for minor in MINORS if str(minor) in FORBIDDEN and not any(f in pages for f in money_forms(minor))]
    assert not unseen, f"the owner's pages show none of these amounts' forms: {unseen}"


@pytest.mark.parametrize(
    "html",
    [
        '<script type="application/json">{{"spending_minor": {n}, "count": 1}}</script>',
        '<script type="application/json">{{"net_minor": -{n}}}</script>',
        '<progress class="progress" value="{n}" max="100"></progress>',
        "<td>{n}</td>",
        "date,amount\n2026-10-15,-{n},USD\n",
        '<td class="tabular-nums">+${comma}</td>',
        '<td class="tabular-nums">\N{MINUS SIGN}${comma}</td>',
        '{{"amount": "{plain}"}}',
    ],
)
def test_a_rendered_canary_amount_trips_the_route_sweep(html):
    for minor in MINORS:
        comma, plain, bare = money_forms(minor)
        assert _leaks(HttpResponse(html.format(n=bare, comma=comma, plain=plain)), OUTSIDER_FORBIDDEN)


def test_the_owners_exports_trip_the_route_sweep(db):
    world = build_report_world()
    archive = HttpResponse(write_export_zip(world.owner), content_type="application/zip")
    streamed = _client(world.owner).post(reverse("transaction-export"))
    transactions = HttpResponse(b"".join(streamed.streaming_content), content_type=streamed["Content-Type"])
    for response in (archive, transactions):
        assert str(UNCATEGORIZED_MINOR) in _leaks(response, OUTSIDER_FORBIDDEN)
