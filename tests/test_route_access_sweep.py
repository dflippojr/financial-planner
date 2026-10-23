"""Every URL route, checked for private-versus-household access (#551).

Each route from the URL resolver is classified in exactly one table:
OBJECT_ROUTES take an object id in the path, PAGE_ROUTES read financial data
without one, and EXEMPT_ROUTES read none. A member asking for another
member's private object, and an outsider asking for a household's shared
object, get the app's 404 (or a 405 for a method the view never accepts),
see no canary and change nothing. The owner reaches every seeded object with
the same URL, so the denials cannot pass because the seed is unreachable.

A new route needs an entry here; test_every_route_is_classified_once fails
until it has one.
"""
import io
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from collections.abc import Callable

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Model
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from finance.category_suggestion_services import snapshot_hash
from finance.models import (
    Account,
    AiConversation,
    AiConversationMessage,
    AiProposal,
    AiProviderConnection,
    Alert,
    AuditEvent,
    BalanceSnapshot,
    Budget,
    BudgetAmount,
    BulkEditUndo,
    Category,
    CategoryRule,
    CategorySuggestion,
    Household,
    ImportBatch,
    MemberSession,
    Membership,
    Passkey,
    PlannedItem,
    Receipt,
    RefundLink,
    RuleApplication,
    RuleApplicationEntry,
    SavedCsvMapping,
    SavedTransactionFilter,
    SavingsGoal,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransactionTag,
)
from finance.category_services import ensure_household_categories
from finance.policy_services import accept_policy, current_policy
from finance.reauth import RECENT_AUTH_SESSION_KEY
from finance.year_end import CSV_SECTIONS
from tests.privacy_world import (
    CANARY,
    FORBIDDEN,
    body_text,
    build_report_world,
    make_person,
    money_forms,
    seed_transaction,
    signed_in,
)


HOUSEHOLD_CANARY = "SYN-HOUSEHOLD"
SNAPSHOT_MINOR = 246_813
MANUAL_MINOR = 135_791
SHARED_MINOR = 271_828
BUDGET_MINOR = 314_159
SPLIT_MINOR = 123_457
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 24

# The member must see none of the owner's private canaries; the outsider must
# also see none of the household's.
PRIVATE_FORBIDDEN = (
    *FORBIDDEN,
    *money_forms(SNAPSHOT_MINOR),
    *money_forms(MANUAL_MINOR),
    *money_forms(BUDGET_MINOR),
    *money_forms(SPLIT_MINOR),
)
OUTSIDER_FORBIDDEN = (*PRIVATE_FORBIDDEN, HOUSEHOLD_CANARY, *money_forms(SHARED_MINOR))

DENIED = (404, 405)
PAGE_ANSWERS = (200, 302, 404)
# A form that names an object the requester cannot see may also be refused as invalid.
PAGE_POST_ANSWERS = (200, 302, 400, 404)


@dataclass(frozen=True)
class ObjectRoute:
    """A route with an object id in its path.

    kwargs builds the URL kwargs from one side of the world: the owner's
    private objects, or the household's shared ones. sides names the sides
    the object exists on: personal objects (passkeys, chats) are never
    shared, and household objects (categories, CSV mappings) are never
    private. data is minimal synthetic form data for the POST. post_only
    marks a view that refuses GET, so the owner check posts instead.
    """

    kwargs: Callable[[SimpleNamespace], dict]
    sides: tuple[str, ...] = ("private", "shared")
    data: Callable[[SimpleNamespace], dict] = field(default=lambda _side: {})
    post_only: bool = False
    # The sides where the owner's request succeeds, when that is not every side:
    # an account can only be shared while private and unshared while shared.
    owner_sides: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PageRoute:
    """A route without an object id that reads financial data.

    query lists the GET parameters the view reads for scoping; each gets the
    owner's private object id (PROBE names which object, an account by
    default). form lists POST fields that carry ids, for views that take a
    form; None when the view takes no POST worth probing.
    """

    query: tuple[str, ...] = ()
    form: tuple[str, ...] | None = None
    extra: dict = field(default_factory=dict)
    kwargs: tuple[dict, ...] = ({},)


def _account(side):
    return {"account_id": side.account.pk}


def _txn(side):
    return {"transaction_id": side.txn.pk}


def _manual(side):
    return {"transaction_id": side.manual.pk}


def _receipt(side):
    return {"transaction_id": side.txn.pk, "receipt_id": side.receipt.pk}


def _snapshot(side):
    return {"account_id": side.account.pk, "snapshot_id": side.snapshot.pk}


PERSONAL = ("private",)
HOUSEHOLD = ("shared",)

OBJECT_ROUTES = {
    "chat-delete": ObjectRoute(lambda s: {"conversation_id": s.conversation.pk}, PERSONAL, post_only=True),
    "chat-proposal-apply": ObjectRoute(lambda s: {"proposal_id": s.proposal.pk}, PERSONAL, post_only=True),
    "chat-proposal-dismiss": ObjectRoute(lambda s: {"proposal_id": s.proposal.pk}, PERSONAL, post_only=True),
    "chat-turn": ObjectRoute(lambda s: {"turn_id": s.turn.pk}, PERSONAL),
    "spending-category-detail": ObjectRoute(lambda s: {"category_id": s.category.pk}, HOUSEHOLD),
    "passkey-delete": ObjectRoute(lambda s: {"passkey_id": s.passkey.pk}, PERSONAL, post_only=True),
    "revoke-session": ObjectRoute(lambda s: {"session_id": s.session.pk}, PERSONAL, post_only=True),
    "category-rule-detail": ObjectRoute(
        lambda s: {"rule_id": s.rule.pk}, data=lambda s: {"action": "apply"}
    ),
    "category-rule-application-reverse": ObjectRoute(
        lambda s: {"application_id": s.application.pk}, post_only=True
    ),
    "csv-mapping-edit": ObjectRoute(
        lambda s: {"mapping_id": s.mapping.pk}, HOUSEHOLD, data=lambda s: {"name": "SYN-renamed mapping"}
    ),
    "alert-mark-read": ObjectRoute(lambda s: {"alert_id": s.alert.pk}, PERSONAL, post_only=True),
    "account-rename": ObjectRoute(_account, data=lambda s: {"name": "SYN-renamed"}, post_only=True),
    "account-share": ObjectRoute(
        _account, data=lambda s: {"share_mode": "co_owned"}, post_only=True, owner_sides=("private",)
    ),
    "account-share-mode": ObjectRoute(
        _account,
        data=lambda s: {"share_mode": "lent", "confirm_give_up_ownership": "on"},
        post_only=True,
        owner_sides=("shared",),
    ),
    "account-unshare": ObjectRoute(_account, post_only=True, owner_sides=("shared",)),
    "account-archive": ObjectRoute(_account, post_only=True),
    "account-delete": ObjectRoute(_account, data=lambda s: {"confirm_name": s.account.name}),
    "account-balances": ObjectRoute(
        _account, data=lambda s: {"snapshot_date": "2026-01-01", "amount": "1.00", "note": "SYN"}
    ),
    "account-pair-loan": ObjectRoute(
        lambda s: {"account_id": s.loan.pk}, data=lambda s: {"secured_asset": ""}, post_only=True
    ),
    "account-debt-terms": ObjectRoute(
        lambda s: {"account_id": s.card.pk},
        data=lambda s: {"apr_percent": "1.000", "minimum_payment": "1.00"},
        post_only=True,
    ),
    "account-snapshot-edit": ObjectRoute(
        _snapshot, data=lambda s: {"snapshot_date": "2026-01-01", "amount": "1.00", "note": "SYN"}
    ),
    "account-snapshot-delete": ObjectRoute(_snapshot, post_only=True),
    "csv-import-preview": ObjectRoute(_account, data=lambda s: {"action": "cancel"}),
    "csv-import-undo": ObjectRoute(
        lambda s: {"account_id": s.account.pk, "batch_id": s.txn.import_batch_id}, post_only=True
    ),
    "transaction-saved-filter-apply": ObjectRoute(lambda s: {"filter_id": s.saved_filter.pk}, PERSONAL),
    "transaction-saved-filter-delete": ObjectRoute(
        lambda s: {"filter_id": s.saved_filter.pk}, PERSONAL, post_only=True
    ),
    "transaction-bulk-undo": ObjectRoute(lambda s: {"undo_id": s.undo.pk}, PERSONAL, post_only=True),
    "suggestion-accept": ObjectRoute(lambda s: {"suggestion_id": s.suggestion.pk}, PERSONAL, post_only=True),
    "suggestion-reject": ObjectRoute(lambda s: {"suggestion_id": s.suggestion.pk}, PERSONAL, post_only=True),
    "transaction-edit": ObjectRoute(
        _txn, data=lambda s: {"transaction_date": "2026-01-01", "description": "SYN-edited", "amount": "1.00"}
    ),
    "transaction-delete": ObjectRoute(_manual, post_only=True),
    "transaction-receipt-upload": ObjectRoute(
        _txn, data=lambda s: {"receipt": SimpleUploadedFile("syn.png", PNG, content_type="image/png")}, post_only=True
    ),
    "transaction-receipt-download": ObjectRoute(_receipt),
    "transaction-receipt-delete": ObjectRoute(_receipt, post_only=True),
    "transaction-note-tags": ObjectRoute(_txn, data=lambda s: {"note": "SYN-note"}, post_only=True),
    "transaction-categorize": ObjectRoute(_txn, data=lambda s: {"category": s.category.pk}, post_only=True),
    "transaction-split": ObjectRoute(_txn, data=lambda s: {"part_count": "2"}, post_only=True),
    "transaction-unsplit": ObjectRoute(_txn, data=lambda s: {"category": s.category.pk}, post_only=True),
    "transaction-split-part-category": ObjectRoute(
        lambda s: {"transaction_id": s.txn.pk, "part_id": s.part.pk},
        data=lambda s: {f"part{s.part.pk}-category": s.category.pk},
        post_only=True,
    ),
    "transaction-link-refund": ObjectRoute(_manual, data=lambda s: {"original": s.txn.pk}, post_only=True),
    "planned-item-edit": ObjectRoute(lambda s: {"item_id": s.planned.pk}),
    "planned-item-disable": ObjectRoute(lambda s: {"item_id": s.planned.pk}, post_only=True),
    "planned-item-enable": ObjectRoute(lambda s: {"item_id": s.planned.pk}, post_only=True),
    "budget-edit": ObjectRoute(lambda s: {"budget_id": s.budget.pk}),
    "budget-archive": ObjectRoute(lambda s: {"budget_id": s.budget.pk}, post_only=True),
    "budget-rollover-toggle": ObjectRoute(
        lambda s: {"budget_id": s.budget.pk}, data=lambda s: {"enabled": "1"}, post_only=True
    ),
    "budget-rollover-reset": ObjectRoute(lambda s: {"budget_id": s.budget.pk}),
    "savings-goal-edit": ObjectRoute(lambda s: {"goal_id": s.goal.pk}),
    "savings-goal-complete": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True),
    "savings-goal-reopen": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True),
    "savings-goal-archive": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True),
    "savings-goal-unarchive": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True),
}

# The object a query or form parameter names, when it is not an account.
PROBE = {
    "actor": "owner",
    "c": "conversation",
    "conversation_id": "conversation",
    "include": "card",
    "merge_series": "series",
    "add_series": "series",
    "suggestion_id": "suggestion",
    "transaction_id": "txn",
    "eligible_id": "txn",
}

FILTERS = ("account", "tag", "scope")

PAGE_ROUTES = {
    "settings-audit": PageRoute(query=("actor",)),
    "chat": PageRoute(query=("c",)),
    "more": PageRoute(),
    "chat-popover": PageRoute(query=("c",)),
    "chat-send": PageRoute(form=("conversation_id",), extra={"prompt": "SYN question"}),
    "home": PageRoute(query=FILTERS),
    "spending-by-category": PageRoute(query=FILTERS),
    "spending-category-uncategorized": PageRoute(query=FILTERS),
    "net-worth": PageRoute(query=("scope",)),
    "account-settings": PageRoute(),
    "simplefin-connections": PageRoute(),
    "invite": PageRoute(),
    "category-list": PageRoute(),
    "category-rule-list": PageRoute(query=("category",)),
    "tag-list": PageRoute(),
    "csv-mapping-list": PageRoute(),
    "settings-alerts": PageRoute(),
    "settings-data": PageRoute(),
    "account-export": PageRoute(form=()),
    "settings-ai": PageRoute(),
    "alert-list": PageRoute(),
    "account-list": PageRoute(),
    "csv-import": PageRoute(form=("account_id",)),
    "transaction-list": PageRoute(query=(*FILTERS, "category")),
    "transaction-export": PageRoute(form=("account",)),
    "transaction-saved-filter-create": PageRoute(form=("account",), extra={"name": "SYN filter"}),
    "transaction-bulk-preview": PageRoute(
        form=("transaction_id",), extra={"action": "append_note", "note_line": "SYN"}
    ),
    "transaction-bulk-apply": PageRoute(
        form=("transaction_id", "eligible_id"), extra={"action": "append_note", "note_line": "SYN"}
    ),
    "suggest-categories": PageRoute(form=("account",), extra={"category": "uncategorized"}),
    "suggestion-accept-all": PageRoute(form=("suggestion_id",)),
    "transaction-add": PageRoute(
        form=("account",),
        extra={"transaction_date": "2026-01-01", "direction": "out", "amount": "1.00", "description": "SYN"},
    ),
    "transfer-review": PageRoute(),
    "recurring-review": PageRoute(query=("merge_series", "add_series")),
    "recurring-create": PageRoute(
        form=("transaction_id",), extra={"step": "preview", "name": "SYN", "cadence": "monthly"}
    ),
    "planned-items": PageRoute(query=("scope",)),
    "bills-calendar": PageRoute(query=("scope",), form=("accounts",)),
    "debt-payoff": PageRoute(query=("include",)),
    "monthly-review": PageRoute(),
    "sheet-comparison": PageRoute(query=FILTERS),
    "year-end": PageRoute(query=("scope",)),
    "year-end-csv": PageRoute(query=("scope",), kwargs=tuple({"section": section} for section in CSV_SECTIONS)),
    "budgets": PageRoute(),
    "savings-goals": PageRoute(),
    "savings-goal-plan": PageRoute(query=("scope",)),
    "savings-goal-import": PageRoute(),
}

_SIGN_IN = "sign-in, recovery or re-authentication; runs before any financial data is read"
_REDIRECT = "permanent redirect to a renamed route; reads nothing, and the target's own entry covers it"
_AI_PROVIDER = "AI provider plumbing for the signed-in member's own connection; the path names a provider, not an object"
_OWN = "POST-only action on the signed-in member's own settings or rows; takes no object id"

EXEMPT_ROUTES = {
    "favicon": "static icon redirect",
    "health": "health check; returns service status only",
    "privacy-policy": "public privacy policy text",
    "privacy-policy-version": "public privacy policy text; the id is a policy version, not a member's object",
    "privacy-policy-respond": "records the member's own answer to the policy version shown",
    "member-data-deleted": "static confirmation page shown after a member deletes their data",
    "login": _SIGN_IN,
    "passkey-sign-in": _SIGN_IN,
    "passkey-sign-in-options": _SIGN_IN,
    "passkey-sign-in-assert": _SIGN_IN,
    "google-sign-in": _SIGN_IN,
    "google_login": _SIGN_IN,
    "google_callback": _SIGN_IN,
    "logout": _SIGN_IN,
    "setup": "first-run setup; refuses once a member exists",
    "join": "accepts a household invitation for the signed-in member",
    "recover": _SIGN_IN,
    "reauth": _SIGN_IN,
    "reauth-google": _SIGN_IN,
    "passkey-reauth-options": _SIGN_IN,
    "passkey-reauth-assert": _SIGN_IN,
    "passkey-register-options": "starts registering a passkey for the signed-in member",
    "passkey-register": "registers a passkey for the signed-in member",
    "passkey-require": _OWN,
    "revoke-other-sessions": _OWN,
    "simplefin-sync": _OWN,
    "simplefin-disconnect": _OWN,
    "leave-household": _OWN,
    "delete-my-data": "deletes the signed-in member's own data after typed confirmation",
    "chat-new": _OWN,
    "chat-delete-all": _OWN,
    "chat-warm": _OWN,
    "chat-status": _AI_PROVIDER,
    "alert-mark-all-read": _OWN,
    "monthly-review-regenerate": _OWN,
    "sheet-comparison-delete": _OWN,
    "savings-goal-buffer": _OWN,
    "ai-connect": _AI_PROVIDER,
    "ai-disconnect": _AI_PROVIDER,
    "ai-defaults": _AI_PROVIDER,
    "ai-key-connect": _AI_PROVIDER,
    "ai-key-defaults": _AI_PROVIDER,
    "ai-key-disconnect": _AI_PROVIDER,
    "ai-offer-local": _AI_PROVIDER,
    "ai-offer-local-chat": _AI_PROVIDER,
    "ai-offer-plan": _AI_PROVIDER,
    "ai-plan-start": _AI_PROVIDER,
    "ai-plan-code": _AI_PROVIDER,
    "ai-plan-status": _AI_PROVIDER,
    "ai-plan-unlink": _AI_PROVIDER,
    "ai-shared-local": _AI_PROVIDER,
    # Unnamed routes are keyed by their pattern.
    "settings/": _REDIRECT,
    "account/": _REDIRECT,
    "account/ai/connect/": _REDIRECT,
    "account/ai/disconnect/": _REDIRECT,
    "account/ai/defaults/": _REDIRECT,
    "account/export/": _REDIRECT,
    "account/delete-my-data/": _REDIRECT,
    "invite/": _REDIRECT,
    "household/leave/": _REDIRECT,
    "accounts/connections/": _REDIRECT,
    "accounts/connections/sync/": _REDIRECT,
    "accounts/connections/disconnect/": _REDIRECT,
    "csv-mappings/": _REDIRECT,
    "csv-mappings/<int:mapping_id>/": _REDIRECT,
    "categories/": _REDIRECT,
    "categories/rules/": _REDIRECT,
    "categories/rules/<int:rule_id>/": _REDIRECT,
    "tags/": _REDIRECT,
    "categories/rules/applications/<int:application_id>/reverse/": _REDIRECT,
}

TABLES = {"OBJECT_ROUTES": OBJECT_ROUTES, "PAGE_ROUTES": PAGE_ROUTES, "EXEMPT_ROUTES": EXEMPT_ROUTES}

# Every row a denied request could touch: the target, its children
# (transactions, snapshots, splits, receipts) and the audit and import trail.
TRACKED_MODELS = (
    Account,
    ImportBatch,
    Transaction,
    TransactionSplit,
    TransactionTag,
    TransactionCorrectionHistory,
    RefundLink,
    Receipt,
    BalanceSnapshot,
    Category,
    Tag,
    CategoryRule,
    RuleApplication,
    RuleApplicationEntry,
    SavedCsvMapping,
    SavedTransactionFilter,
    CategorySuggestion,
    BulkEditUndo,
    PlannedItem,
    Budget,
    BudgetAmount,
    SavingsGoal,
    Alert,
    AiConversation,
    AiConversationMessage,
    AiProposal,
    Passkey,
    MemberSession,
    AuditEvent,
)


def _route_keys(patterns, prefix="", namespace=""):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            inner = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from _route_keys(entry.url_patterns, prefix + str(entry.pattern), inner)
        elif isinstance(entry, URLPattern):
            yield f"{namespace}{entry.name}" if entry.name else prefix + str(entry.pattern)


def test_every_route_is_classified_once():
    routes = list(_route_keys(get_resolver().url_patterns))
    duplicated = sorted({key for key in routes if routes.count(key) > 1})
    assert not duplicated, f"routes share a key: {duplicated}"
    problems = []
    for key in routes:
        tables = [name for name, table in TABLES.items() if key in table]
        if not tables:
            problems.append(f"{key} is in no table; add it to OBJECT_ROUTES, PAGE_ROUTES or EXEMPT_ROUTES")
        elif len(tables) > 1:
            problems.append(f"{key} is in {' and '.join(tables)}")
    for name, table in TABLES.items():
        problems += [f"{key} in {name} is no longer a route" for key in table if key not in routes]
    assert not problems, "\n".join(problems)


def test_every_exempt_route_says_why():
    blank = [key for key, reason in EXEMPT_ROUTES.items() if not isinstance(reason, str) or not reason.strip()]
    assert not blank, f"exempt routes without a reason: {blank}"


def test_every_object_route_seeds_a_side():
    for name, route in OBJECT_ROUTES.items():
        assert route.sides and set(route.sides) <= {"private", "shared"}, name
        assert set(route.owner_sides or route.sides) <= set(route.sides), name


def _side_rows(owner, household, account, card, label, amount_minor, when, index):
    """Seed one side: a checking account, card and loan plus everything that hangs off them."""
    is_shared = household is not None
    scope = {"scope": "household", "household": household} if is_shared else {}
    sharing = {"share_mode": Account.ShareMode.CO_OWNED, **scope} if is_shared else {}
    loan = Account.objects.create(name=f"{label} loan", account_type=Account.Type.LOAN, owner=owner, **sharing)
    category = Category.objects.get(household=owner.memberships.get().household, name="Groceries")
    txn = seed_transaction(
        account, owner, amount_minor=-amount_minor, when=when, description=f"{label} split charge", index=index
    )
    TransactionSplit.objects.create(transaction=txn, category=category, amount_minor=-(amount_minor - 100), position=1)
    TransactionSplit.objects.create(
        transaction=txn,
        category=Category.objects.get(household=category.household, name="Dining"),
        amount_minor=-100,
        position=2,
    )
    Transaction.objects.filter(pk=txn.pk).update(category_source=Transaction.CategorySource.SPLIT)
    txn.refresh_from_db()
    manual_batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.MANUAL,
        source_file_sha256="c" * 64,
        date_range_start=when,
        date_range_end=when,
    )
    manual = Transaction.objects.create(
        account=account,
        import_batch=manual_batch,
        transaction_date=when,
        amount_minor=MANUAL_MINOR if not is_shared else 50,
        currency="USD",
        description=f"{label} manual refund",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=f"{index + 1:064x}",
        original_fields={},
    )
    stored = uuid.uuid4().hex
    receipt = Receipt.objects.create(
        transaction=txn,
        original_name=f"{label} receipt.png",
        stored_name=stored,
        content_type=Receipt.ContentType.PNG,
        size_bytes=len(PNG),
        uploaded_by=owner,
    )
    snapshot = BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=when,
        amount_minor=SNAPSHOT_MINOR if not is_shared else SHARED_MINOR,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
        note=f"{label} snapshot",
    )
    rule = CategoryRule.objects.create(
        owner_person=None if is_shared else owner,
        owner_household=household,
        description_contains=f"{label} split",
        category=category,
    )
    application = RuleApplication.objects.create(rule=rule, applied_by=owner)
    RuleApplicationEntry.objects.create(application=application, transaction=manual)
    budget = Budget.objects.create(
        owner=owner,
        category=Category.objects.get(household=category.household, name="Dining" if is_shared else "Groceries"),
        **scope,
    )
    BudgetAmount.objects.create(
        budget=budget,
        effective_month=when.replace(day=1),
        amount_minor=BUDGET_MINOR if not is_shared else SHARED_MINOR,
    )
    return SimpleNamespace(
        owner=owner,
        account=account,
        card=card,
        loan=loan,
        txn=txn,
        part=txn.splits.order_by("position").first(),
        manual=manual,
        receipt=receipt,
        snapshot=snapshot,
        rule=rule,
        application=application,
        budget=budget,
        category=category,
    )


def _personal_rows(side):
    """The owner's own rows that are never shared: chats, alerts, passkeys, sessions."""
    owner, account = side.owner, side.account
    now = timezone.now()
    side.saved_filter = SavedTransactionFilter.objects.create(
        member=owner, name=f"{CANARY} filter", query={"account": str(account.pk)}
    )
    uncategorized = side.manual
    side.suggestion = CategorySuggestion.objects.create(
        member=owner,
        transaction=uncategorized,
        category=side.category,
        provider="local",
        backend="local",
        snapshot_hash=snapshot_hash(uncategorized),
    )
    side.conversation = AiConversation.objects.create(
        member=owner, title=f"{CANARY} chat", used_account_ids=[account.pk], expires_at=now + timedelta(days=1)
    )
    question = AiConversationMessage.objects.create(
        conversation=side.conversation, role=AiConversationMessage.Role.USER, content=f"{CANARY} question"
    )
    side.turn = AiConversationMessage.objects.create(
        conversation=side.conversation,
        role=AiConversationMessage.Role.ASSISTANT,
        content=f"{CANARY} answer",
        reply_to=question,
    )
    side.proposal = AiProposal.objects.create(
        conversation=side.conversation,
        message=side.turn,
        kind=AiProposal.Kind.SET_CATEGORY,
        payload={
            "items": [{"id": side.manual.pk, "category_id": None, "source": "", "label": f"{CANARY} proposal"}],
            "category_id": side.category.pk,
        },
    )
    side.alert = Alert.objects.create(
        recipient=owner,
        kind=Alert.Kind.LARGE_TRANSACTION,
        title=f"{CANARY} alert",
        link=reverse("transaction-list"),
        dedupe_key="syn-canary-alert",
        account=account,
    )
    side.alert.source_accounts.add(account)
    side.passkey = Passkey.objects.create(
        member=owner, name=f"{CANARY} passkey", credential_id=b"syn-credential", public_key=b"syn-key"
    )
    side.session = MemberSession.objects.create(member=owner, session_key="syn-owner-session-key")
    side.undo = BulkEditUndo.objects.create(
        actor=owner,
        expires_at=now + timedelta(hours=1),
        snapshot={
            "action": "append_note",
            "rows": [
                {
                    "id": side.txn.pk,
                    "applied_updated_at": side.txn.updated_at.isoformat(),
                    "action": "append_note",
                    "previous_category_id": None,
                    "previous_category_source": side.txn.category_source,
                    "previous_note": "",
                    "previous_tag_ids": [],
                }
            ],
        },
    )


@pytest.fixture
def world(db, settings, tmp_path):
    settings.RECEIPTS_DIR = str(tmp_path)
    base = build_report_world()
    owner, household, when = base.owner, base.household, base.when
    outsider = make_person("outsider")
    elsewhere = Household.objects.create(name="Synthetic Elsewhere")
    Membership.objects.create(person=outsider, household=elsewhere)
    ensure_household_categories(elsewhere)
    policy = current_policy()
    for person in (owner, base.member, outsider):
        # Everyone may use AI, so the chat views reach their conversation lookups.
        accept_policy(person, policy)
        AiProviderConnection.objects.create(
            owner=person,
            encrypted_token=b"synthetic-unused",
            base_url="http://synthetic.invalid",
            chat_backend="local",
            background_backend="local",
        )

    private = _side_rows(owner, None, base.private, base.card, CANARY, SPLIT_MINOR, when, 100)
    shared_account = Account.objects.create(
        name=f"{HOUSEHOLD_CANARY} checking",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    shared_card = Account.objects.create(
        name=f"{HOUSEHOLD_CANARY} card",
        account_type=Account.Type.CREDIT_CARD,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    shared = _side_rows(owner, household, shared_account, shared_card, HOUSEHOLD_CANARY, SHARED_MINOR, when, 200)
    for side in (private, shared):
        (tmp_path / side.receipt.stored_name).write_bytes(PNG)
    _personal_rows(private)
    private.series = base.series
    private.planned = base.planned
    private.goal = base.goal
    shared.planned = PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.HOUSEHOLD,
        household=household,
        name=f"{HOUSEHOLD_CANARY} planned",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=SHARED_MINOR,
        start_date=when + timedelta(days=40),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    shared.goal = SavingsGoal.objects.create(
        owner=owner,
        scope=SavingsGoal.Scope.HOUSEHOLD,
        household=household,
        name=f"{HOUSEHOLD_CANARY} goal",
        target_amount_minor=SHARED_MINOR,
        target_date=when + timedelta(days=400),
    )
    shared.mapping = SavedCsvMapping.objects.create(
        household=household,
        name=f"{HOUSEHOLD_CANARY} mapping",
        headers=["Date", "Description", "Amount"],
        date_column="Date",
        description_column="Description",
        date_format="iso",
        number_format="dot_none",
        amount_mode="signed",
        amount_column="Amount",
        created_by=owner,
    )
    return SimpleNamespace(
        owner=owner, member=base.member, outsider=outsider, sides={"private": private, "shared": shared}
    )


def _client(person):
    client = signed_in(person)
    # Fresh re-authentication, so actions behind requires_recent_auth reach their lookup.
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()
    return client


def _ledger(requester):
    return {model.__name__: _rows(model, requester) for model in TRACKED_MODELS}


def _rows(model: type[Model], requester):
    rows = model._default_manager.order_by("pk")
    if model is MemberSession:
        # Middleware records the requester's own session on every request.
        rows = rows.exclude(member=requester)
    return [
        {key: bytes(value) if isinstance(value, memoryview) else value for key, value in row.items()}
        for row in rows.values()
    ]


def _changes(before, after):
    return [name for name in before if before[name] != after[name]]


def _text(response):
    if response.get("Content-Type", "").startswith("application/zip"):
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        return "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())
    return body_text(response)


def _leaks(response, forbidden):
    body = _text(response)
    return [needle for needle in forbidden if needle in body]


def _assert_denied(requester, url, data, forbidden):
    client = _client(requester)
    for method in ("get", "post"):
        before = _ledger(requester)
        response = client.post(url, data) if method == "post" else client.get(url)
        label = f"{method.upper()} {url}"
        assert response.status_code in DENIED, f"{label} answered {response.status_code}, not 404"
        assert not _leaks(response, forbidden), f"{label} leaked {_leaks(response, forbidden)}"
        changed = _changes(before, _ledger(requester))
        assert not changed, f"denied {label} changed {changed}"


def _object_cases(sides):
    return [
        pytest.param(name, side, id=f"{name}-{side}")
        for name, route in OBJECT_ROUTES.items()
        for side in route.sides
        if side in sides
    ]


@pytest.mark.parametrize("name, side", _object_cases(("private",)))
def test_member_gets_404_for_another_members_private_object(world, name, side):
    route = OBJECT_ROUTES[name]
    seeded = world.sides[side]
    _assert_denied(world.member, reverse(name, kwargs=route.kwargs(seeded)), route.data(seeded), PRIVATE_FORBIDDEN)


@pytest.mark.parametrize("name, side", _object_cases(("private", "shared")))
def test_outsider_gets_404_for_a_households_object(world, name, side):
    route = OBJECT_ROUTES[name]
    seeded = world.sides[side]
    _assert_denied(world.outsider, reverse(name, kwargs=route.kwargs(seeded)), route.data(seeded), OUTSIDER_FORBIDDEN)


def _owner_cases():
    return [
        pytest.param(name, side, id=f"{name}-{side}")
        for name, route in OBJECT_ROUTES.items()
        for side in route.owner_sides or route.sides
    ]


@pytest.mark.parametrize("name, side", _owner_cases())
def test_owner_reaches_every_seeded_object(world, name, side):
    route = OBJECT_ROUTES[name]
    seeded = world.sides[side]
    url = reverse(name, kwargs=route.kwargs(seeded))
    client = _client(world.owner)
    response = client.post(url, route.data(seeded)) if route.post_only else client.get(url)
    method = "POST" if route.post_only else "GET"
    assert response.status_code not in (404, 405), f"the owner's {method} {url} answered {response.status_code}"


def _probe_value(world, param):
    return str(getattr(world.sides["private"], PROBE.get(param, "account")).pk)


def _page_cases():
    return [
        pytest.param(name, kwargs, who, id=f"{name}-{'-'.join(kwargs.values()) or 'page'}-{who}")
        for name, route in PAGE_ROUTES.items()
        for kwargs in route.kwargs
        for who in ("member", "outsider")
    ]


@pytest.mark.parametrize("name, kwargs, who", _page_cases())
def test_pages_do_not_leak_through_ids_in_the_query(world, name, kwargs, who):
    route = PAGE_ROUTES[name]
    forbidden = PRIVATE_FORBIDDEN if who == "member" else OUTSIDER_FORBIDDEN
    requester = getattr(world, who)
    client = _client(requester)
    url = reverse(name, kwargs=kwargs)
    query = {param: _probe_value(world, param) for param in route.query}
    response = client.get(url, query)
    if route.form is None or response.status_code != 405:
        assert response.status_code in PAGE_ANSWERS, f"GET {url} {query} answered {response.status_code}"
        assert not _leaks(response, forbidden), f"GET {url} {query} leaked {_leaks(response, forbidden)}"
    if route.form is None:
        return
    data = {**route.extra, **{param: _probe_value(world, param) for param in route.form}}
    before = _ledger(requester)
    response = client.post(url, data)
    assert response.status_code in PAGE_POST_ANSWERS, f"POST {url} {data} answered {response.status_code}"
    assert not _leaks(response, forbidden), f"POST {url} {data} leaked {_leaks(response, forbidden)}"
    after = _ledger(requester)
    # The requester may add rows of their own; nothing that existed may change.
    for model, rows in before.items():
        kept = {row["id"]: row for row in after[model]}
        changed = [row["id"] for row in rows if kept.get(row["id"]) != row]
        assert not changed, f"POST {url} changed {model} rows {changed}"
