from datetime import date, timedelta
import pytest
from django.test import Client
from django.urls import reverse
from finance.models import RefundLink, Transaction
from finance.category_services import split_transaction
from tests.test_categorization import make_person, make_household, make_account, make_transaction

pytestmark = pytest.mark.django_db


def setup_refund():
    owner = make_person('search-owner')
    household = make_household(owner)
    account = make_account(owner)
    refund = make_transaction(owner, account, amount_minor=1234)
    client = Client()
    client.force_login(owner.user)
    return owner, household, account, refund, client


def test_refund_search_caps_newest_results_and_links_purchase():
    owner, _, account, refund, client = setup_refund()
    purchases = [make_transaction(owner, account, description=f'Synthetic shop {i}', transaction_date=date(2026, 1, 1) + timedelta(days=i)) for i in range(30)]
    url = reverse('transaction-edit', args=(refund.pk,))
    initial = client.get(url)
    assert b'Search purchases' in initial.content
    assert b'name="original_part"' not in initial.content
    response = client.get(url, {'refund_search': 'Synthetic shop'})
    assert [p.pk for p in response.context['refund_results']] == [p.pk for p in reversed(purchases[5:])]
    assert b'Synthetic shop 0 ' not in response.content
    selected = client.get(url, {'original': purchases[-1].pk})
    assert str(purchases[-1].pk).encode() in selected.content
    linked = client.post(reverse('transaction-link-refund', args=(refund.pk,)), {'original': purchases[-1].pk})
    assert linked.status_code == 302
    assert RefundLink.objects.get(refund=refund).original_id == purchases[-1].pk
    assert b'Search purchases' not in client.get(url).content


def test_refund_search_filters_amount_account_kind_status_and_visibility():
    owner, household, account, refund, client = setup_refund()
    other = make_person('private-owner')
    from finance.models import Membership
    Membership.objects.create(person=other, household=household)
    private = make_account(other)
    second = make_account(owner)
    match = make_transaction(owner, account, amount_minor=-1234, description='Needle purchase')
    hidden = make_transaction(other, private, amount_minor=-1234, description='Needle private secret')
    make_transaction(owner, second, amount_minor=-1234, description='Needle other account')
    make_transaction(owner, account, amount_minor=-2222, description='Needle wrong amount')
    make_transaction(owner, account, amount_minor=1234, description='Needle inflow')
    make_transaction(owner, account, amount_minor=-1234, description='Needle investment', kind=Transaction.Kind.INVESTMENT_ACTIVITY)
    archived = make_transaction(owner, account, amount_minor=-1234, description='Needle archived')
    from django.utils import timezone
    archived.status = Transaction.Status.ARCHIVED
    archived.archived_at = timezone.now()
    archived.save()
    url = reverse('transaction-edit', args=(refund.pk,))
    response = client.get(url, {'refund_search': 'needle', 'refund_amount': '12.34', 'refund_same_account': 'on'})
    assert [p.pk for p in response.context['refund_results']] == [match.pk]
    for data in [{'refund_search': 'private secret'}, {'original': hidden.pk}, {'original': 'invalid'}, {'refund_search': '', 'refund_amount': 'oops'}]:
        response = client.get(url, data)
        assert b'Needle private secret' not in response.content
    rejected = client.post(reverse('transaction-link-refund', args=(refund.pk,)), {'original': hidden.pk})
    assert rejected.status_code == 200
    assert not RefundLink.objects.filter(refund=refund).exists()


@pytest.mark.parametrize('amount', [-1234, 0])
def test_nonpositive_transaction_has_no_refund_search(amount):
    owner, _, account, _, client = setup_refund()
    purchase = make_transaction(owner, account, amount_minor=amount)
    response = client.get(reverse('transaction-edit', args=(purchase.pk,)))
    assert b'Search purchases' not in response.content
    assert b'name="original"' not in response.content
    assert b'name="original_part"' not in response.content
    assert len(response.content) < 60000


def test_split_selection_loads_only_selected_purchase_parts():
    owner, household, account, refund, client = setup_refund()
    category = household.categories.get(name='Groceries')
    purchases = [make_transaction(owner, account, amount_minor=-2000, description=f'Synthetic split {i}') for i in range(2)]
    for purchase in purchases:
        split_transaction(owner, purchase.pk, [{'category_id': category.pk, 'amount_minor': -1000}, {'category_id': category.pk, 'amount_minor': -1000}])
    response = client.get(reverse('transaction-edit', args=(refund.pk,)), {'original': purchases[0].pk})
    form = response.context['refund_form']
    assert {p.pk for p in form.fields['original_part'].queryset} == set(purchases[0].splits.values_list('pk', flat=True))
    part = purchases[0].splits.first()
    response = client.post(reverse('transaction-link-refund', args=(refund.pk,)), {'original': purchases[0].pk, 'original_part': part.pk})
    assert response.status_code == 302
    assert RefundLink.objects.get(refund=refund).original_part_id == part.pk
