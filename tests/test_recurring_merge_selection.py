import re

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.models import RecurringSeries
from tests.test_recurring_review import make_household, make_person, make_series


@pytest.fixture
def review_client(monkeypatch):
    # Exercise rendering of persisted review states without detection replacing them.
    monkeypatch.setattr("finance.views.refresh_recurring_series", lambda *args, **kwargs: None)
    owner = make_person("owner")
    make_household(owner)
    client = Client()
    client.force_login(owner.user)
    return owner, client


@pytest.mark.django_db
@pytest.mark.parametrize("count", [100, 343])
def test_merge_options_render_only_for_the_selected_series(review_client, count):
    owner, client = review_client
    rows = RecurringSeries.objects.bulk_create([
        RecurringSeries(
            person=owner,
            merchant_key=f"synthetic {index}",
            display_name=f"Synthetic {index:03}",
            cadence=RecurringSeries.Cadence.MONTHLY,
            typical_amount_minor=-1000,
            status=RecurringSeries.Status.SUGGESTED,
            confidence=RecurringSeries.Confidence.HIGH,
            reasons=["synthetic"],
            fingerprint=f"{index:064x}",
        )
        for index in range(count)
    ])
    url = reverse("recurring-review")
    page = client.get(url)
    html = page.content.decode()
    assert page.status_code == 200
    assert 'name="target_id"' not in html
    assert html.count('?merge_series=') == count
    # Bound the full HTML per series, catching a return to quadratic rendering.
    assert len(page.content) < count * 10000

    selected = client.get(url, {"merge_series": rows[0].pk})
    html = selected.content.decode()
    assert selected.status_code == 200
    assert html.count('name="target_id"') == 1
    assert [int(pk) for pk in re.findall(r'<option value="(\d+)">', html)] == [
        row.pk for row in rows[1:]
    ]
    assert len(selected.content) < count * 10000
    assert 'name="action" value="merge"' in html
    assert f'name="series_id" value="{rows[0].pk}"' in html


@pytest.mark.django_db
def test_merge_picker_excludes_unavailable_series_and_escapes_names(review_client):
    owner, client = review_client
    other = make_person("other")
    source = make_series(owner, name="Synthetic Source", cadence=RecurringSeries.Cadence.MONTHLY)
    target = make_series(owner, name="Synthetic <Target> & Co", cadence=RecurringSeries.Cadence.ANNUAL)
    hidden = make_series(other, name="Synthetic Private", cadence=RecurringSeries.Cadence.MONTHLY)
    excluded = []
    for name, updates in [
        ("Dismissed", {"status": RecurringSeries.Status.DISMISSED}),
        ("Inactive", {"is_active": False}),
        ("Cancelled", {"cancelled_at": timezone.now()}),
    ]:
        row = make_series(owner, name=f"Synthetic {name}", cadence=RecurringSeries.Cadence.MONTHLY)
        RecurringSeries.objects.filter(pk=row.pk).update(**updates)
        excluded.append(row)
    url = reverse("recurring-review")
    page = client.get(url, {"merge_series": source.pk})
    html = page.content.decode()
    assert page.status_code == 200
    assert re.findall(r'<option value="(\d+)">', html) == [str(target.pk)]
    assert "Synthetic &lt;Target&gt; &amp; Co (Annual)" in html
    assert "Synthetic Private" not in html
    for raw_id in [hidden.pk, *[row.pk for row in excluded], 999999, "invalid", ""]:
        assert client.get(url, {"merge_series": raw_id}).status_code == 404


@pytest.mark.django_db
def test_single_series_has_no_merge_control(review_client):
    owner, client = review_client
    source = make_series(owner, name="Synthetic Only", cadence=RecurringSeries.Cadence.MONTHLY)
    url = reverse("recurring-review")
    for params in [{}, {"merge_series": source.pk}]:
        page = client.get(url, params)
        assert page.status_code == 200
        assert b'?merge_series=' not in page.content
        assert b'name="target_id"' not in page.content
