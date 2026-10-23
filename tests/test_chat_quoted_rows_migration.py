"""Conversations stored before 0075 are purged like newer ones once a quoted row is out of sight (#569).

They never recorded which planned items or budgets they quoted, so the upgrade
records every one their member could have seen, and the purge fails closed.
"""

from datetime import date, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from finance.budget_services import save_budget
from finance.lifecycle_services import leave_household
from finance.models import AiConversation, Budget, Person, PlannedItem
from finance.planning_services import save_planned_item

BEFORE = [("finance", "0074_balance_snapshot_original_currency")]
MONTH = date(2026, 3, 1)


class _Seed:
    """Synthetic rows written through the 0074 models: Alex shares a home with Blair and Casey."""

    def __init__(self, apps):
        self.apps = apps
        self.home = self.model("Household").objects.create(name="Synthetic Home")
        joined = timezone.now() - timedelta(days=90)
        self.alex, self.blair, self.casey, self.drew = (
            self.person(name, joined) for name in ("Alex", "Blair", "Casey", "Drew")
        )
        self.category = self.model("Category").objects.create(household=self.home, name="Synthetic Groceries")
        self.item = self.model("PlannedItem").objects.create(
            owner=self.alex, scope="household", household=self.home, name="ZQPLANNEDROW", kind="expense",
            amount_minor=432_109, currency="USD", start_date=MONTH, cadence="monthly",
        )
        self.budget = self.model("Budget").objects.create(
            owner=self.alex, scope="household", household=self.home, category=self.category
        )
        self.model("BudgetAmount").objects.create(budget=self.budget, effective_month=MONTH, currency="USD",
                                                  amount_minor=432_109)

    def model(self, name):
        return self.apps.get_model("finance", name)

    def person(self, name, joined):
        user = self.apps.get_model("auth", "User").objects.create(username=f"synthetic-{name.lower()}")
        person = self.model("Person").objects.create(user=user, display_name=f"Synthetic {name}")
        self.model("Membership").objects.create(person=person, household=self.home, joined_at=joined)
        return person

    def conversation(self, member, *, tool_calls=1, began=None):
        conversation = self.model("AiConversation").objects.create(
            member=member, title="Synthetic chat", used_account_ids=[], turn_count=1, tool_call_count=tool_calls,
            expires_at=timezone.now() + timedelta(days=30),
        )
        if began is not None:
            self.model("AiConversation").objects.filter(pk=conversation.pk).update(created_at=began)
        self.model("AiConversationMessage").objects.create(
            conversation=conversation, role="assistant", content="ZQPLANNEDROW costs 4,321.09 USD"
        )
        return conversation.pk


@pytest.fixture
def upgraded():
    """Seed at 0074 with `seed(apps)`, migrate to head, and roll the schema back afterwards."""
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    state = {}

    def migrate(seed):
        executor.migrate(BEFORE)
        state["rows"] = seed(_Seed(executor.loader.project_state(BEFORE).apps))
        MigrationExecutor(connection).migrate(head)
        return state["rows"]

    try:
        yield migrate
    finally:
        MigrationExecutor(connection).migrate(head)


def _plan_payload(item, scope):
    return {"scope": scope, "name": item.name, "kind": item.kind, "amount_minor": item.amount_minor,
            "start_date": item.start_date, "end_date": item.end_date, "cadence": item.cadence}


def _budget_payload(budget, scope):
    return {"scope": scope, "category": budget.category, "amount_minor": 432_109, "effective_month": MONTH,
            "rollover_enabled": False}


def _seed_two_chats(seed):
    return seed, seed.conversation(seed.blair), seed.conversation(seed.alex)


@pytest.mark.django_db(transaction=True)
def test_making_a_planned_item_private_purges_a_pre_0075_chat_that_may_have_quoted_it(upgraded):
    seed, blairs, alexs = upgraded(_seed_two_chats)

    item = PlannedItem.objects.get(pk=seed.item.pk)
    save_planned_item(Person.objects.get(pk=seed.alex.pk), _plan_payload(item, "private"), item=item)

    assert not AiConversation.objects.filter(pk=blairs).exists()
    # Alex still sees the item, so Alex's conversation is kept.
    assert AiConversation.objects.filter(pk=alexs).exists()


@pytest.mark.django_db(transaction=True)
def test_making_a_budget_private_purges_a_pre_0075_chat_that_may_have_quoted_it(upgraded):
    seed, blairs, alexs = upgraded(_seed_two_chats)

    budget = Budget.objects.get(pk=seed.budget.pk)
    save_budget(Person.objects.get(pk=seed.alex.pk), _budget_payload(budget, "private"), budget=budget)

    assert not AiConversation.objects.filter(pk=blairs).exists()
    assert AiConversation.objects.filter(pk=alexs).exists()


@pytest.mark.django_db(transaction=True)
def test_leaving_purges_the_leavers_pre_0075_chat_over_household_plans(upgraded):
    seed, blairs, alexs = upgraded(_seed_two_chats)

    leave_household(Person.objects.get(pk=seed.blair.pk))

    assert not AiConversation.objects.filter(pk=blairs).exists()
    assert AiConversation.objects.filter(pk=alexs).exists()


def _seed_history(seed):
    began = timezone.now() - timedelta(days=30)
    # Alex's private rows changed after Drew's conversation began but before the others.
    seed.model("PlannedItem").objects.create(
        owner=seed.alex, scope="private", name="Synthetic private plan", kind="income", amount_minor=100,
        currency="USD", start_date=MONTH, cadence="annual",
    )
    own = seed.model("Budget").objects.create(owner=seed.alex, scope="private")
    chats = {
        "current": seed.conversation(seed.blair),
        "no_tools": seed.conversation(seed.blair, tool_calls=0),
        # Casey left after this conversation began, so it may hold the home's rows.
        "left": seed.conversation(seed.casey, began=began),
        # Drew's conversation began before Alex's private plan was last changed.
        "changed_since": seed.conversation(seed.drew, began=began),
        "proposal": seed.conversation(seed.alex),
    }
    seed.model("Membership").objects.filter(person=seed.casey).update(ended_at=timezone.now() - timedelta(days=1))
    turn = seed.model("AiConversationMessage").objects.filter(conversation_id=chats["proposal"]).get()
    seed.model("AiProposal").objects.create(conversation_id=chats["proposal"], message=turn, kind="set_budget",
                                            payload={"budget_id": own.pk, "amount_minor": 100})
    return seed, chats, own


@pytest.mark.django_db(transaction=True)
def test_upgrade_records_what_each_chat_could_have_seen_and_purges_what_is_out_of_sight(upgraded):
    seed, chats, own = upgraded(_seed_history)

    survivors = {row.pk: row for row in AiConversation.objects.all()}
    assert set(survivors) == {chats["current"], chats["no_tools"], chats["proposal"]}
    quoted = (survivors[chats["current"]].used_planned_item_ids, survivors[chats["current"]].used_budget_ids)
    assert quoted == ([seed.item.pk], [seed.budget.pk])
    # A conversation that ran no tool quoted nothing.
    assert (survivors[chats["no_tools"]].used_planned_item_ids, survivors[chats["no_tools"]].used_budget_ids) == ([], [])
    # A set-budget proposal names its budget exactly.
    assert survivors[chats["proposal"]].used_budget_ids == sorted([seed.budget.pk, own.pk])
