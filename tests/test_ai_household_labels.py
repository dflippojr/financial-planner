"""Household category, tag and budget names stay out of AI until household consent is complete (#521)."""

import json
from datetime import date

import pytest
from django.contrib.auth import get_user_model

from finance.ai_jobs import process_due_jobs
from finance.ai_services import connect_harness, set_defaults
from finance.ai_tools import (
    WITHHELD_CATEGORY_NAME,
    list_budgets,
    run_tool,
    search_transactions,
    spending_by_category,
)
from finance.budget_services import save_budget
from finance.category_services import add_category, ensure_household_categories
from finance.category_suggestion_services import queue_remaining_uncategorized
from finance.chat_proposals import proposal_tools
from finance.chat_services import send_message
from finance.models import Budget, Category, Membership, Person
from finance.monthly_review_ai import facts_payload_for_ai
from finance.policy_services import accept_policy, current_policy
from finance.tag_services import set_transaction_note_and_tags
from tests.test_chat import PASSWORD, TOKEN, add_txn, checking, harness as harness_fixture, make_member  # noqa: F401

pytestmark = pytest.mark.django_db

CUSTOM = "Synthetic Beta Counseling"
TAG = "Synthetic Beta Tag"
DAY = date(2026, 3, 4)
RANGE = {"date_from": "2026-03-01", "date_to": "2026-03-31"}


def _household_without_consent():
    """Alpha accepted the AI policy; beta, a current member, has not."""
    _user, alpha, household = make_member("alpha")
    beta_user = get_user_model().objects.create_user(username="beta", password=PASSWORD)
    beta = Person.objects.create(user=beta_user, display_name="Beta Example")
    Membership.objects.create(person=beta, household=household)
    ensure_household_categories(household)
    custom = add_category(alpha, CUSTOM)
    private = checking(alpha, household, "Synthetic Alpha Private", private=True)
    row = add_txn(private, alpha, DAY, -4200, "Synthetic private counseling", category=custom)
    set_transaction_note_and_tags(alpha, row.pk, note="", tag_ids=[], new_tag_name=TAG)
    save_budget(alpha, {"scope": Budget.Scope.PRIVATE, "category": custom, "amount_minor": 10000,
                        "effective_month": date(2026, 3, 1), "rollover_enabled": False})
    return alpha, beta, household, custom, private


def _texts(alpha):
    return {
        "spending": spending_by_category(alpha, RANGE).text,
        "budgets": list_budgets(alpha, {"month": "2026-03"}).text,
        "search": search_transactions(alpha, {**RANGE, "q": "counseling"}).text,
    }


def test_tools_withhold_household_category_tag_and_budget_names_until_consent_is_complete():
    alpha, beta, _household, _custom, _private = _household_without_consent()

    for name, text in _texts(alpha).items():
        assert CUSTOM not in text, name
        assert TAG not in text, name
    assert WITHHELD_CATEGORY_NAME in json.loads(_texts(alpha)["spending"])["rows"][0]["name"]
    assert json.loads(_texts(alpha)["budgets"])["rows"][0]["name"] == WITHHELD_CATEGORY_NAME
    # The member's own private row still counts; only the label is withheld.
    assert json.loads(_texts(alpha)["spending"])["total_spending_minor"] == 4200

    accept_policy(beta, current_policy())

    texts = _texts(alpha)
    assert CUSTOM in texts["spending"]
    assert CUSTOM in texts["budgets"]
    assert WITHHELD_CATEGORY_NAME not in texts["spending"] + texts["budgets"]


def test_search_answers_a_withheld_category_like_a_missing_one():
    alpha, beta, _household, custom, _private = _household_without_consent()
    groceries = Category.objects.get(household=custom.household, name="Groceries")

    def answer(args):
        result = search_transactions(alpha, {**RANGE, **args})
        return result.ok, result.text

    assert answer({"category": CUSTOM}) == answer({"category": "Synthetic Missing Name"})
    assert answer({"category_id": custom.pk}) == answer({"category_id": 999999})
    assert answer({"category": "Groceries"})[0] is True
    assert answer({"category_id": groceries.pk})[0] is True

    accept_policy(beta, current_policy())

    ok, text = answer({"category": CUSTOM})
    assert ok and "Synthetic private counseling" in text


def test_propose_tools_answer_a_withheld_category_like_a_missing_one(harness):
    _state, url = harness
    alpha, beta, _household, _custom, private = _household_without_consent()
    connect_harness(alpha, base_url=url, token=TOKEN)
    row = add_txn(private, alpha, DAY, -1500, "Synthetic private cafe")
    conversation = send_message(alpha, "hello")
    turn = conversation.messages.get(status="pending")
    tools = proposal_tools(conversation, turn)
    calls = {
        "propose_set_category": {"transaction_ids": [row.pk], "category": "{}"},
        "propose_create_rule": {"description_contains": "cafe", "category": "{}", "owner": "personal"},
        "propose_set_budget": {"category": "{}", "month": "2026-03", "amount_minor": 5000},
    }

    def answer(name, category):
        args = {key: value.format(category) if value == "{}" else value for key, value in calls[name].items()}
        result = run_tool(alpha, tools, name, args)
        return result.ok, result.text

    for name in calls:
        assert answer(name, CUSTOM) == answer(name, "Synthetic Missing Name"), name
        assert answer(name, CUSTOM)[0] is False, name

    accept_policy(beta, current_policy())

    assert answer("propose_create_rule", CUSTOM)[0] is True


def test_category_suggestion_prompt_offers_only_built_in_names_until_consent_is_complete(harness, monkeypatch):
    state, url = harness
    alpha, beta, _household, _custom, private = _household_without_consent()
    connect_harness(alpha, base_url=url, token=TOKEN)
    set_defaults(alpha, chat_backend="local", background_backend="local")
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    add_txn(private, alpha, DAY, -900, "Synthetic uncategorized coffee")
    state.session_answer = json.dumps({"suggestions": []})

    queue_remaining_uncategorized(alpha)
    process_due_jobs()

    prompt = "\n".join(state.session_prompts)
    assert "Synthetic uncategorized coffee" in prompt
    assert "name=Groceries" in prompt
    assert CUSTOM not in prompt

    accept_policy(beta, current_policy())
    add_txn(private, alpha, date(2026, 3, 5), -950, "Synthetic second coffee")
    queue_remaining_uncategorized(alpha)
    process_due_jobs()

    assert CUSTOM in "\n".join(state.session_prompts)


def test_restricted_review_facts_withhold_household_category_names_on_unusual_flags(monkeypatch):
    alpha, beta, household, custom, _private = _household_without_consent()
    groceries = Category.objects.get(household=household, name="Groceries")

    def flags(_person, _month, accounts):
        return [
            {"kind": "category", "name": custom.name, "key": custom.pk, "month_minor": 4200},
            {"kind": "category", "name": "Groceries", "key": groceries.pk, "month_minor": 3100},
        ]

    monkeypatch.setattr("finance.unusual_spending.compute_unusual_flags", flags)
    facts = {"month": "2026-03", "month_label": "March 2026", "unusual": []}

    payload = facts_payload_for_ai(alpha, facts)

    assert [flag["name"] for flag in payload["unusual"]] == [WITHHELD_CATEGORY_NAME, "Groceries"]
    assert CUSTOM not in json.dumps(payload)


def test_spending_by_category_answers_a_withheld_category_id_like_a_missing_one():
    alpha, beta, _household, custom, _private = _household_without_consent()

    def rows(category):
        return json.loads(spending_by_category(alpha, {**RANGE, "category": category}).text)["rows"]

    assert rows(str(custom.pk)) == rows("999999") == []
    assert rows(CUSTOM) == []

    accept_policy(beta, current_policy())

    assert [row["name"] for row in rows(str(custom.pk))] == [CUSTOM]
