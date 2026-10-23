"""Every URL route, checked for private-versus-household access (#551).

Each route from the URL resolver is classified in exactly one table:
OBJECT_ROUTES take an object id in the path, PAGE_ROUTES read financial data
without one, and EXEMPT_ROUTES read none. A member asking for another
member's private object, and an outsider asking for a household's shared
object, get the app's 404 (or a 405 for a method the view never accepts),
see no canary and change nothing. The owner reaches every seeded object with
the same URL, so the denials cannot pass because the seed is unreachable.
A page that names an object in its POST body gets the same probe through
its actions, and a page that accepts POST must declare its body ids or say
why it names none. So must an object route: its body probes post to the
requester's own object and name another member's or household's in the
body.

A new route needs an entry here; test_every_route_is_classified_once fails
until it has one.
"""
import io
import re
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
from finance.csv_import.saved_mappings import SAVED_PROFILE_PREFIX
from finance.models import (
    Account,
    AccountLink,
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
    RecurringExclusion,
    RecurringPriceAcknowledgement,
    RecurringSeries,
    RecurringSeriesMember,
    RefundLink,
    RuleApplication,
    RuleApplicationEntry,
    SavedCsvMapping,
    SavedTransactionFilter,
    SavingsGoal,
    SimpleFinConnection,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransactionTag,
    TransferPair,
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
# A form field's choices leave the object out, so the page shows the form again.
INVALID_FORM = (200,)
# The view redirects back whether or not the form was valid.
REDIRECTED = (302,)


@dataclass(frozen=True)
class BodyProbe:
    """A POST to the requester's own object that names another object in its body.

    path builds the URL kwargs from the requester's own rows, so the path
    lookup passes; data builds the form from one side of the world and those
    rows. The member names the owner's private object and the outsider either
    side's; both are refused and change nothing. refused lists the answers
    the route gives. The owner's same POST on the first side changes
    something, or passes owner_check when it writes nothing to the database,
    so the probe reaches the body's lookup.
    """

    path: Callable[[SimpleNamespace], dict]
    data: Callable[[SimpleNamespace, SimpleNamespace], dict]
    sides: tuple[str, ...] = ("private", "shared")
    refused: tuple[int, ...] = INVALID_FORM
    owner_check: Callable[[object], bool] | None = None


@dataclass(frozen=True)
class ObjectRoute:
    """A route with an object id in its path.

    kwargs builds the URL kwargs from one side of the world: the owner's
    private objects, or the household's shared ones. sides names the sides
    the object exists on: personal objects (passkeys, chats) are never
    shared, and household objects (categories, CSV mappings) are never
    private. data is minimal synthetic form data for the POST. post_only
    marks a view that refuses GET, so the owner check posts instead. body
    lists the further object ids its POST body may name. A route that
    accepts POST needs body or a body_reason saying why its body names no
    object.
    """

    kwargs: Callable[[SimpleNamespace], dict]
    sides: tuple[str, ...] = ("private", "shared")
    data: Callable[[SimpleNamespace], dict] = field(default=lambda _side: {})
    post_only: bool = False
    # The sides where the owner's request succeeds, when that is not every side:
    # an account can only be shared while private and unshared while shared.
    owner_sides: tuple[str, ...] | None = None
    body: dict[str, BodyProbe] = field(default_factory=dict)
    body_reason: str = ""


@dataclass(frozen=True)
class FormAction:
    """A POST a page takes that names an object in its body.

    data builds the form from one side of the world and the requester's own
    rows, so every other id in it is one the requester may use. The member
    posts it naming the owner's private object and the outsider naming either
    side's; both are refused and change nothing. refused lists the answers
    the route gives: 404 from a lookup, or 200 when a form field's choices
    leave the object out. The owner's same POST on the first side changes
    something, so the probe reaches the lookup.
    """

    data: Callable[[SimpleNamespace, SimpleNamespace], dict]
    sides: tuple[str, ...] = ("private", "shared")
    refused: tuple[int, ...] = (404,)


@dataclass(frozen=True)
class PageRoute:
    """A route without an object id that reads financial data.

    query lists the GET parameters the view reads for scoping. Each is probed
    on its own, on top of a valid baseline (the BASELINE parameters named in
    baseline), so an unrelated validation error cannot leave the report
    empty: an id parameter gets the owner's object id (PROBE names which
    object, an account by default) and scope gets each scope. owner_canary
    marks a page whose baseline shows the owner a canary, which proves the
    probe would see a leak. form lists POST fields that carry ids, for views
    whose form may still succeed on the requester's own rows; None when the
    view takes no such form. owner_post checks that the owner's same POST,
    with ids they may use, gets past the view's other checks, so the probe
    reaches the authorization. actions lists the POSTs that act on one named
    object and must be refused outright. A page that accepts POST needs form,
    actions or a post_reason saying why its body names no object.
    """

    query: tuple[str, ...] = ()
    baseline: tuple[str, ...] = ()
    owner_canary: bool = False
    form: tuple[str, ...] | None = None
    extra: dict = field(default_factory=dict)
    kwargs: tuple[dict, ...] = ({},)
    owner_post: Callable[[SimpleNamespace, object], bool] | None = None
    actions: dict[str, FormAction] = field(default_factory=dict)
    post_reason: str = ""


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

RULE_FORM = {"owner_kind": "personal", "description_contains": "SYN rule", "priority": "0", "enabled": "on"}
PLANNED_FORM = {
    "name": "SYN planned",
    "kind": "expense",
    "amount": "1.00",
    "start_date": "2026-01-01",
    "cadence": "monthly",
    "scope": "private",
}
GOAL_FORM = {"name": "SYN goal", "target_amount": "1.00"}
MAPPING_FORM = {
    "name": "SYN mapping",
    "date_column": "Date",
    "description_column": "Description",
    "date_format": "iso",
    "number_format": "dot_none",
    "amount_mode": "signed",
    "amount_column": "Amount",
}
CSV = b"Date,Description,Amount\n2026-01-01,SYN upload,-1.00\n"

_PATH_ONLY = "acts on the object in its path alone; the body names no other object"
_VALUES = "the body carries only values such as a name, date, amount, mode or flag, never an object id"


def _own_txn(own):
    return {"transaction_id": own.charge.pk}


def _own_split(own):
    return {"transaction_id": own.split.pk}


def _upload(s, own):
    return {
        "action": "upload",
        "csv_file": SimpleUploadedFile("syn.csv", CSV, content_type="text/csv"),
        "import_profile": f"{SAVED_PROFILE_PREFIX}{s.mapping.pk}",
    }


def _uploaded(response):
    form = response.context and response.context.get("upload_form")
    return response.status_code == 200 and form is not None and form.is_valid()


OBJECT_ROUTES = {
    "chat-delete": ObjectRoute(
        lambda s: {"conversation_id": s.conversation.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "chat-proposal-apply": ObjectRoute(
        lambda s: {"proposal_id": s.proposal.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "chat-proposal-dismiss": ObjectRoute(
        lambda s: {"proposal_id": s.proposal.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "chat-turn": ObjectRoute(lambda s: {"turn_id": s.turn.pk}, PERSONAL),
    "spending-category-detail": ObjectRoute(lambda s: {"category_id": s.category.pk}, HOUSEHOLD),
    "passkey-delete": ObjectRoute(
        lambda s: {"passkey_id": s.passkey.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "revoke-session": ObjectRoute(
        lambda s: {"session_id": s.session.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "category-rule-detail": ObjectRoute(
        lambda s: {"rule_id": s.rule.pk},
        data=lambda s: {"action": "apply"},
        body={
            "save-account": BodyProbe(
                lambda own: {"rule_id": own.rule.pk},
                lambda s, own: {**RULE_FORM, "action": "save", "account": s.account.pk, "category": own.category.pk},
            ),
            "save-category": BodyProbe(
                lambda own: {"rule_id": own.rule.pk},
                lambda s, own: {**RULE_FORM, "action": "save", "category": s.category.pk},
                HOUSEHOLD,
            ),
            # The application need not belong to the rule in the path.
            "reverse": BodyProbe(
                lambda own: {"rule_id": own.rule.pk},
                lambda s, own: {"action": "reverse", "application_id": s.application.pk},
                refused=(404,),
            ),
        },
    ),
    "category-rule-application-reverse": ObjectRoute(
        lambda s: {"application_id": s.application.pk}, post_only=True, body_reason=_PATH_ONLY
    ),
    "csv-mapping-edit": ObjectRoute(
        lambda s: {"mapping_id": s.mapping.pk},
        HOUSEHOLD,
        data=lambda s: {"name": "SYN-renamed mapping"},
        # The member's own mapping is the household's, so the member reaches it legitimately.
        body={
            "default_accounts": BodyProbe(
                lambda own: {"mapping_id": own.mapping.pk},
                lambda s, own: {**MAPPING_FORM, "default_accounts": [s.account.pk]},
            ),
        },
    ),
    "alert-mark-read": ObjectRoute(
        lambda s: {"alert_id": s.alert.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "account-rename": ObjectRoute(
        _account, data=lambda s: {"name": "SYN-renamed"}, post_only=True, body_reason=_VALUES
    ),
    "account-share": ObjectRoute(
        _account,
        data=lambda s: {"share_mode": "co_owned"},
        post_only=True,
        owner_sides=("private",),
        body_reason=_VALUES,
    ),
    "account-share-mode": ObjectRoute(
        _account,
        data=lambda s: {"share_mode": "lent", "confirm_give_up_ownership": "on"},
        post_only=True,
        owner_sides=("shared",),
        body_reason=_VALUES,
    ),
    "account-unshare": ObjectRoute(_account, post_only=True, owner_sides=("shared",), body_reason=_PATH_ONLY),
    "account-archive": ObjectRoute(_account, post_only=True, body_reason=_PATH_ONLY),
    "account-delete": ObjectRoute(_account, data=lambda s: {"confirm_name": s.account.name}, body_reason=_VALUES),
    "account-balances": ObjectRoute(
        _account,
        data=lambda s: {"snapshot_date": "2026-01-01", "amount": "1.00", "note": "SYN"},
        body_reason=_VALUES,
    ),
    "account-pair-loan": ObjectRoute(
        lambda s: {"account_id": s.loan.pk},
        data=lambda s: {"secured_asset": ""},
        post_only=True,
        body={
            "secured_asset": BodyProbe(
                lambda own: {"account_id": own.loan.pk},
                lambda s, own: {"secured_asset": s.asset.pk},
                refused=REDIRECTED,
            ),
        },
    ),
    "account-debt-terms": ObjectRoute(
        lambda s: {"account_id": s.card.pk},
        data=lambda s: {"apr_percent": "1.000", "minimum_payment": "1.00"},
        post_only=True,
        body_reason=_VALUES,
    ),
    "account-snapshot-edit": ObjectRoute(
        _snapshot,
        data=lambda s: {"snapshot_date": "2026-01-01", "amount": "1.00", "note": "SYN"},
        body_reason=_VALUES,
    ),
    "account-snapshot-delete": ObjectRoute(_snapshot, post_only=True, body_reason=_PATH_ONLY),
    "csv-import-preview": ObjectRoute(
        _account,
        data=lambda s: {"action": "cancel"},
        # The other actions name a stage by its token, which only the member's own session holds.
        body={
            "import_profile": BodyProbe(
                lambda own: {"account_id": own.account.pk}, _upload, HOUSEHOLD, owner_check=_uploaded
            ),
        },
    ),
    "csv-import-undo": ObjectRoute(
        lambda s: {"account_id": s.account.pk, "batch_id": s.txn.import_batch_id},
        post_only=True,
        body_reason=_PATH_ONLY,
    ),
    "transaction-saved-filter-apply": ObjectRoute(lambda s: {"filter_id": s.saved_filter.pk}, PERSONAL),
    "transaction-saved-filter-delete": ObjectRoute(
        lambda s: {"filter_id": s.saved_filter.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "transaction-bulk-undo": ObjectRoute(
        lambda s: {"undo_id": s.undo.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "suggestion-accept": ObjectRoute(
        lambda s: {"suggestion_id": s.suggestion.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "suggestion-reject": ObjectRoute(
        lambda s: {"suggestion_id": s.suggestion.pk}, PERSONAL, post_only=True, body_reason=_PATH_ONLY
    ),
    "transaction-edit": ObjectRoute(
        _txn,
        data=lambda s: {"transaction_date": "2026-01-01", "description": "SYN-edited", "amount": "1.00"},
        body_reason="the correction form carries only a date, description and amount; tags post to note-tags",
    ),
    "transaction-delete": ObjectRoute(_manual, post_only=True, body_reason=_PATH_ONLY),
    "transaction-receipt-upload": ObjectRoute(
        _txn,
        data=lambda s: {"receipt": SimpleUploadedFile("syn.png", PNG, content_type="image/png")},
        post_only=True,
        body_reason="the body carries only the uploaded file",
    ),
    "transaction-receipt-download": ObjectRoute(_receipt),
    "transaction-receipt-delete": ObjectRoute(_receipt, post_only=True, body_reason=_PATH_ONLY),
    "transaction-note-tags": ObjectRoute(
        _txn,
        data=lambda s: {"note": "SYN-note"},
        post_only=True,
        body={"tags": BodyProbe(_own_txn, lambda s, own: {"tags": [s.tag.pk]}, HOUSEHOLD)},
    ),
    "transaction-categorize": ObjectRoute(
        _txn,
        data=lambda s: {"category": s.category.pk},
        post_only=True,
        body={"category": BodyProbe(_own_txn, lambda s, own: {"category": s.category.pk}, HOUSEHOLD, REDIRECTED)},
    ),
    "transaction-split": ObjectRoute(
        _txn,
        data=lambda s: {"part_count": "2"},
        post_only=True,
        body={
            "part_category": BodyProbe(
                _own_txn,
                lambda s, own: {
                    "part_count": "2",
                    "part_0_category": s.category.pk,
                    "part_0_amount": "-40.00",
                    "part_1_category": own.category.pk,
                    "part_1_amount": "-3.21",
                },
                HOUSEHOLD,
            ),
        },
    ),
    "transaction-unsplit": ObjectRoute(
        _txn,
        data=lambda s: {"category": s.category.pk},
        post_only=True,
        body={"category": BodyProbe(_own_split, lambda s, own: {"category": s.category.pk}, HOUSEHOLD, REDIRECTED)},
    ),
    "transaction-split-part-category": ObjectRoute(
        lambda s: {"transaction_id": s.txn.pk, "part_id": s.part.pk},
        data=lambda s: {f"part{s.part.pk}-category": s.category.pk},
        post_only=True,
        body={
            "category": BodyProbe(
                lambda own: {"transaction_id": own.split.pk, "part_id": own.part.pk},
                lambda s, own: {f"part{own.part.pk}-category": s.category.pk},
                HOUSEHOLD,
                REDIRECTED,
            ),
        },
    ),
    "transaction-link-refund": ObjectRoute(
        _manual,
        data=lambda s: {"original": s.txn.pk},
        post_only=True,
        body={
            "original": BodyProbe(
                lambda own: {"transaction_id": own.refund.pk}, lambda s, own: {"original": s.charges[0].pk}
            ),
            # A split original needs one of its own parts.
            "original_part": BodyProbe(
                lambda own: {"transaction_id": own.refund.pk},
                lambda s, own: {"original": s.txn.pk, "original_part": s.part.pk},
            ),
        },
    ),
    "planned-item-edit": ObjectRoute(
        lambda s: {"item_id": s.planned.pk},
        body={
            "category": BodyProbe(
                lambda own: {"item_id": own.planned.pk},
                lambda s, own: {**PLANNED_FORM, "category": s.category.pk},
                HOUSEHOLD,
            ),
            "replaces_series": BodyProbe(
                lambda own: {"item_id": own.planned.pk},
                lambda s, own: {**PLANNED_FORM, "replaces_series": s.series.pk},
            ),
        },
    ),
    "planned-item-disable": ObjectRoute(lambda s: {"item_id": s.planned.pk}, post_only=True, body_reason=_PATH_ONLY),
    "planned-item-enable": ObjectRoute(lambda s: {"item_id": s.planned.pk}, post_only=True, body_reason=_PATH_ONLY),
    "budget-edit": ObjectRoute(
        lambda s: {"budget_id": s.budget.pk},
        body_reason="the category field is disabled on edit and the view keeps the budget's own scope and category",
    ),
    "budget-archive": ObjectRoute(lambda s: {"budget_id": s.budget.pk}, post_only=True, body_reason=_VALUES),
    "budget-rollover-toggle": ObjectRoute(
        lambda s: {"budget_id": s.budget.pk}, data=lambda s: {"enabled": "1"}, post_only=True, body_reason=_VALUES
    ),
    "budget-rollover-reset": ObjectRoute(lambda s: {"budget_id": s.budget.pk}, body_reason=_VALUES),
    "savings-goal-edit": ObjectRoute(
        lambda s: {"goal_id": s.goal.pk},
        body={
            # A goal depends only on a goal of the same scope.
            "depends_on": BodyProbe(
                lambda own: {"goal_id": own.goal.pk},
                lambda s, own: {**GOAL_FORM, "scope": s.scope, "depends_on": s.goal.pk},
            ),
            "linked_account": BodyProbe(
                lambda own: {"goal_id": own.goal.pk},
                lambda s, own: {**GOAL_FORM, "scope": "private", "linked_account": s.account.pk},
            ),
        },
    ),
    "savings-goal-complete": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True, body_reason=_PATH_ONLY),
    "savings-goal-reopen": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True, body_reason=_PATH_ONLY),
    "savings-goal-archive": ObjectRoute(lambda s: {"goal_id": s.goal.pk}, post_only=True, body_reason=_PATH_ONLY),
    "savings-goal-unarchive": ObjectRoute(
        lambda s: {"goal_id": s.goal.pk}, post_only=True, body_reason=_PATH_ONLY
    ),
}

# The object a query or form parameter names, when it is not an account.
PROBE = {
    "actor": "owner",
    "c": "conversation",
    "conversation_id": "conversation",
    "include": "card",
    "category": "category",
    "tag": "tag",
    "merge_series": "series",
    "add_series": "series",
    "suggestion_id": "suggestion",
    "transaction_id": "charges",
    "eligible_id": "charges",
}

FILTERS = ("account", "tag", "scope")
WINDOW = ("date_from", "date_to")
REMOTE_ID = "SYN-REMOTE-1"


def _series_action(action, series="series"):
    return FormAction(lambda s, own: {"action": action, "series_id": getattr(s, series).pk})


def _pair_action(action, pair):
    return FormAction(lambda s, own: {"action": action, "pair_id": getattr(s, pair).pk})


RECURRING_ACTIONS = {
    "confirm": _series_action("confirm", "suggested_series"),
    "dismiss": _series_action("dismiss", "suggested_series"),
    "merge-source": FormAction(
        lambda s, own: {"action": "merge", "series_id": s.series.pk, "target_id": own.series.pk}
    ),
    "merge-target": FormAction(
        lambda s, own: {"action": "merge", "series_id": own.series.pk, "target_id": s.series.pk}
    ),
    "remove": FormAction(
        lambda s, own: {"action": "remove", "series_id": s.series.pk, "transaction_id": s.series_charge.pk}
    ),
    "add": FormAction(
        lambda s, own: {
            "action": "add",
            "series_id": own.series.pk,
            "transaction_id": [charge.pk for charge in s.charges],
        }
    ),
    "cancel": _series_action("cancel"),
    "undo_cancel": _series_action("undo_cancel", "cancelled_series"),
    "confirm_resume": _series_action("confirm_resume", "cancelled_series"),
    "keep_cancelled": _series_action("keep_cancelled", "cancelled_series"),
    "dismiss_price": _series_action("dismiss_price"),
}

PAGE_ROUTES = {
    "settings-audit": PageRoute(query=("actor",)),
    "chat": PageRoute(query=("c",), owner_canary=True),
    "more": PageRoute(),
    "chat-popover": PageRoute(query=("c",)),
    "chat-send": PageRoute(
        form=("conversation_id",),
        extra={"prompt": "SYN question"},
        owner_post=lambda world, _response: world.sides["private"]
        .conversation.messages.filter(content="SYN question")
        .exists(),
    ),
    "home": PageRoute(query=FILTERS, baseline=WINDOW, owner_canary=True),
    "spending-by-category": PageRoute(query=FILTERS, baseline=WINDOW, owner_canary=True),
    "spending-category-uncategorized": PageRoute(query=FILTERS, baseline=WINDOW, owner_canary=True),
    "net-worth": PageRoute(query=("scope",), baseline=WINDOW, owner_canary=True),
    "account-settings": PageRoute(
        owner_canary=True, post_reason="password and Google sign-in for the signed-in member; names no object"
    ),
    "simplefin-connections": PageRoute(
        actions={
            "link": FormAction(
                lambda s, own: {"intent": "link", "sf_id_0": REMOTE_ID, "action_0": "link", "account_id_0": s.account.pk}
            ),
        }
    ),
    "invite": PageRoute(post_reason="creates an invitation to the signed-in member's own household"),
    "category-list": PageRoute(
        actions={
            "rename": FormAction(
                lambda s, own: {"action": "rename", "category_id": s.category.pk, "name": "SYN renamed"}, HOUSEHOLD
            ),
        }
    ),
    "category-rule-list": PageRoute(
        query=("category",),
        owner_canary=True,
        actions={
            "account": FormAction(
                lambda s, own: {**RULE_FORM, "account": s.account.pk, "category": own.category.pk},
                refused=INVALID_FORM,
            ),
            "category": FormAction(
                lambda s, own: {**RULE_FORM, "category": s.category.pk}, HOUSEHOLD, refused=INVALID_FORM
            ),
        },
    ),
    "tag-list": PageRoute(
        actions={
            "rename": FormAction(lambda s, own: {"action": "rename", "tag_id": s.tag.pk, "name": "SYN renamed"}, HOUSEHOLD),
            "archive": FormAction(lambda s, own: {"action": "archive", "tag_id": s.tag.pk}, HOUSEHOLD),
        }
    ),
    "csv-mapping-list": PageRoute(
        actions={"delete": FormAction(lambda s, own: {"action": "delete", "mapping_id": s.mapping.pk}, HOUSEHOLD)}
    ),
    "settings-alerts": PageRoute(post_reason="the signed-in member's own notice settings; names no object"),
    "settings-data": PageRoute(),
    "account-export": PageRoute(
        form=(), post_reason="exports the signed-in member's own data; the body names no object"
    ),
    "settings-ai": PageRoute(post_reason="records the member's answer to a policy version, not a member's object"),
    "alert-list": PageRoute(owner_canary=True),
    "account-list": PageRoute(owner_canary=True, post_reason="adds an account for the signed-in member"),
    "csv-import": PageRoute(form=("account_id",), owner_canary=True),
    "transaction-list": PageRoute(query=(*FILTERS, "category"), baseline=WINDOW, owner_canary=True),
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
        owner_canary=True,
    ),
    "transfer-review": PageRoute(
        actions={
            "confirm": _pair_action("confirm", "suggested_pair"),
            "dismiss": _pair_action("dismiss", "suggested_pair"),
            "undo": _pair_action("undo", "marked_pair"),
        }
    ),
    "recurring-review": PageRoute(
        query=("merge_series", "add_series"), owner_canary=True, actions=RECURRING_ACTIONS
    ),
    "recurring-create": PageRoute(
        form=("transaction_id",),
        extra={"step": "preview", "name": "SYN", "cadence": "monthly"},
        owner_canary=True,
        owner_post=lambda _world, response: response.context["preview"] is not None,
    ),
    "planned-items": PageRoute(
        query=("scope",),
        owner_canary=True,
        actions={
            "category": FormAction(
                lambda s, own: {**PLANNED_FORM, "category": s.category.pk}, HOUSEHOLD, refused=INVALID_FORM
            ),
            "replaces_series": FormAction(
                lambda s, own: {**PLANNED_FORM, "replaces_series": s.series.pk}, refused=INVALID_FORM
            ),
        },
    ),
    "bills-calendar": PageRoute(query=("scope",), form=("accounts",), owner_canary=True),
    "debt-payoff": PageRoute(query=("include",), owner_canary=True),
    "monthly-review": PageRoute(baseline=("month",), owner_canary=True),
    "sheet-comparison": PageRoute(
        query=FILTERS,
        owner_canary=True,
        post_reason="the member's own uploaded sheet; its token names a stage in the member's own session",
    ),
    "year-end": PageRoute(query=("scope",), baseline=("year",), owner_canary=True),
    "year-end-csv": PageRoute(
        query=("scope",), baseline=("year",), kwargs=tuple({"section": section} for section in CSV_SECTIONS)
    ),
    "budgets": PageRoute(
        baseline=("month",),
        actions={
            "category": FormAction(
                lambda s, own: {
                    "scope": "household",
                    "category": s.category.pk,
                    "amount": "1.00",
                    "effective_month": "2026-01",
                },
                HOUSEHOLD,
                refused=INVALID_FORM,
            ),
        },
    ),
    "savings-goals": PageRoute(
        owner_canary=True,
        actions={
            # A goal depends only on a goal of the same scope.
            "depends_on": FormAction(
                lambda s, own: {**GOAL_FORM, "scope": s.scope, "depends_on": s.goal.pk}, refused=INVALID_FORM
            ),
            "linked_account": FormAction(
                lambda s, own: {**GOAL_FORM, "scope": "private", "linked_account": s.account.pk},
                refused=INVALID_FORM,
            ),
        },
    ),
    "savings-goal-plan": PageRoute(query=("scope",), owner_canary=True),
    "savings-goal-import": PageRoute(
        post_reason="the member's own goal file; its token names a stage in the member's own session"
    ),
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
    "chat-status": "status of the signed-in member's own chat backend; takes no object id",
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
    TransferPair,
    RecurringSeries,
    RecurringSeriesMember,
    RecurringExclusion,
    RecurringPriceAcknowledgement,
    SimpleFinConnection,
    AccountLink,
    AuditEvent,
)


def _route_keys(patterns, prefix="", namespace=""):
    """Each route's key, with its full pattern."""
    for entry in patterns:
        if isinstance(entry, URLResolver):
            inner = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from _route_keys(entry.url_patterns, prefix + str(entry.pattern), inner)
        elif isinstance(entry, URLPattern):
            pattern = prefix + str(entry.pattern)
            yield (f"{namespace}{entry.name}" if entry.name else pattern), pattern


def test_every_route_is_classified_once():
    routes = [key for key, _pattern in _route_keys(get_resolver().url_patterns)]
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


# Exempt routes whose path id names no member's object.
EXEMPT_IDS = (
    "privacy-policy-version",
    "csv-mappings/<int:mapping_id>/",
    "categories/rules/<int:rule_id>/",
    "categories/rules/applications/<int:application_id>/reverse/",
)


def test_every_route_with_an_id_in_its_path_is_an_object_route():
    """A route that gains an id keeps its old entry, so its new shape must move it to OBJECT_ROUTES."""
    misplaced = [
        key
        for key, pattern in _route_keys(get_resolver().url_patterns)
        if re.search(r"<(int|uuid):", pattern) and key not in OBJECT_ROUTES and key not in EXEMPT_IDS
    ]
    assert not misplaced, f"routes with an id in the path that are not in OBJECT_ROUTES: {misplaced}"


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
    asset = Account.objects.create(
        name=f"{label} house", account_type=Account.Type.REAL_ESTATE, owner=owner, **sharing
    )
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
    # Two plain charges: a manual recurring series needs at least two.
    charges = [
        seed_transaction(
            account,
            owner,
            amount_minor=-amount_minor,
            when=when - timedelta(days=30 * month),
            description=f"{label} subscription",
            index=index + 10 + month,
        )
        for month in (0, 1)
    ]
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
    side = SimpleNamespace(
        owner=owner,
        scope="household" if is_shared else "private",
        account=account,
        card=card,
        loan=loan,
        asset=asset,
        txn=txn,
        charges=charges,
        part=txn.splits.order_by("position").first(),
        manual=manual,
        receipt=receipt,
        snapshot=snapshot,
        rule=rule,
        application=application,
        budget=budget,
        category=category,
    )
    _transfer_pairs(side, when, index + 50)
    side.suggested_series = _series(owner, f"{label} suggestion", RecurringSeries.Status.SUGGESTED, [], index + 60)
    side.cancelled_series = _series(
        owner,
        f"{label} cancelled",
        RecurringSeries.Status.CONFIRMED,
        [manual],
        index + 61,
        # Cancelled before the side's charges, so keeping it cancelled drops the later one.
        cancelled_at=timezone.now() - (timezone.localdate() - when) - timedelta(days=15),
    )
    return side


def _transfer_pairs(side, when, index):
    """A suggested and an auto-marked card payment from the side's checking account to its card."""
    pairs = []
    for offset, status in enumerate((TransferPair.Status.SUGGESTED, TransferPair.Status.AUTO_MARKED)):
        amount = 5_150 + 100 * offset
        legs = [
            seed_transaction(
                target, side.owner, amount_minor=sign * amount, when=when, description="SYN card payment", index=index + 2 * offset + leg
            )
            for leg, (target, sign) in enumerate(((side.account, -1), (side.card, 1)))
        ]
        pairs.append(
            TransferPair.objects.create(
                leg_a=legs[0],
                leg_b=legs[1],
                status=status,
                kind=TransferPair.Kind.CARD_PAYMENT,
                confidence=TransferPair.Confidence.HIGH,
                reasons=["synthetic"],
            )
        )
    side.suggested_pair, side.marked_pair = pairs


def _series(person, name, status, members, index, cancelled_at=None):
    series = RecurringSeries.objects.create(
        person=person,
        merchant_key=name.lower(),
        display_name=name,
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1_000,
        status=status,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=f"{index:064x}",
        cancelled_at=cancelled_at,
    )
    for member in members:
        RecurringSeriesMember.objects.create(series=series, transaction=member)
    return series


def _own_rows(person, household, when, index):
    """The requester's own rows: the ids a probe may use, and the objects a body probe posts to."""
    account = Account.objects.create(
        name=f"Synthetic {person.display_name} spending", account_type=Account.Type.CHECKING, owner=person
    )
    charge = seed_transaction(account, person, amount_minor=-4_321, when=when, description="Synthetic own stream", index=index)
    SimpleFinConnection.objects.create(owner=person, encrypted_access_url=b"synthetic-unused")
    category = household.categories.get(name="Groceries")
    split = seed_transaction(account, person, amount_minor=-4_000, when=when, description="Synthetic own split", index=index + 10)
    for position, name in enumerate(("Groceries", "Dining"), start=1):
        TransactionSplit.objects.create(
            transaction=split, category=household.categories.get(name=name), amount_minor=-2_000, position=position
        )
    Transaction.objects.filter(pk=split.pk).update(category_source=Transaction.CategorySource.SPLIT)
    name = f"Synthetic {person.display_name}"
    return SimpleNamespace(
        account=account,
        charge=charge,
        split=split,
        part=split.splits.order_by("position").first(),
        refund=seed_transaction(account, person, amount_minor=1_234, when=when, description="Synthetic own refund", index=index + 20),
        loan=Account.objects.create(name=f"{name} loan", account_type=Account.Type.LOAN, owner=person),
        series=_series(person, "Synthetic own stream", RecurringSeries.Status.CONFIRMED, [charge], index),
        category=category,
        rule=CategoryRule.objects.create(owner_person=person, description_contains="Synthetic own split", category=category),
        planned=PlannedItem.objects.create(
            owner=person,
            name=f"{name} planned",
            kind=PlannedItem.Kind.EXPENSE,
            amount_minor=1_000,
            start_date=when,
            cadence=PlannedItem.Cadence.MONTHLY,
        ),
        goal=SavingsGoal.objects.create(owner=person, name=f"{name} goal", target_amount_minor=1_000),
        mapping=SavedCsvMapping.objects.create(
            household=household,
            name=f"{name} mapping",
            headers=["Date", "Description", "Amount"],
            date_column="Date",
            description_column="Description",
            date_format="iso",
            number_format="dot_none",
            amount_mode="signed",
            amount_column="Amount",
            created_by=person,
        ),
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
def world(db, settings, tmp_path, monkeypatch):
    settings.RECEIPTS_DIR = str(tmp_path)
    # Every connection lists one synthetic SimpleFIN account, and a sync never leaves the test.
    remote = {"id": REMOTE_ID, "name": "SYN remote", "institution": "", "type": "", "currency": "USD"}
    monkeypatch.setattr("finance.simplefin_views.load_remote_accounts", lambda connection: ([remote], []))
    monkeypatch.setattr("finance.simplefin_views.sync_connection", lambda *args: {"result": "SYN synced"})
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
            offer_local_chat=True,
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
    tag = Tag.objects.create(household=household, name="SYN trip")
    for side in (private, shared):
        (tmp_path / side.receipt.stored_name).write_bytes(PNG)
        TransactionTag.objects.create(transaction=side.txn, tag=tag)
        side.tag = tag
    _personal_rows(private)
    private.series = base.series
    private.series_charge = base.secret_charge
    shared.series = _series(owner, f"{HOUSEHOLD_CANARY} stream", RecurringSeries.Status.CONFIRMED, [shared.txn], 262)
    shared.series_charge = shared.txn
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
    own = {
        "owner": _own_rows(owner, household, when, 300),
        "member": _own_rows(base.member, household, when, 301),
        "outsider": _own_rows(outsider, elsewhere, when, 302),
    }
    return SimpleNamespace(
        owner=owner,
        member=base.member,
        outsider=outsider,
        when=when,
        sides={"private": private, "shared": shared},
        own=own,
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
    # A bare minor amount such as 7777 can turn up inside a UUID or a timestamp, so it leaks only as a whole number.
    return [
        needle
        for needle in forbidden
        if (re.search(rf"(?<!\w){needle}(?!\w)", body) if needle.isdigit() else needle in body)
    ]


def _assert_denied(requester, url, data, forbidden):
    client = _client(requester)
    for method in ("get", "post"):
        before = _ledger(requester)
        response = client.post(url, data) if method == "post" else client.get(url)
        label = f"{method.upper()} {url}"
        assert response.status_code in DENIED, f"{label} answered {response.status_code}, not 404"
        leaked = _leaks(response, forbidden)
        assert not leaked, f"{label} leaked {leaked}"
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


def _body_cases():
    """The member names the owner's private object; the outsider names either side's."""
    return [
        pytest.param(name, probe, side, who, id=f"{name}-{probe}-{side}-{who}")
        for name, route in OBJECT_ROUTES.items()
        for probe, body in route.body.items()
        for who, sides in (("member", ("private",)), ("outsider", ("private", "shared")))
        for side in body.sides
        if side in sides
    ]


@pytest.mark.parametrize("name, probe, side, who", _body_cases())
def test_body_ids_refuse_another_members_or_households_object(world, name, probe, side, who):
    body = OBJECT_ROUTES[name].body[probe]
    requester = getattr(world, who)
    forbidden = PRIVATE_FORBIDDEN if who == "member" else OUTSIDER_FORBIDDEN
    own = world.own[who]
    url = reverse(name, kwargs=body.path(own))
    data = body.data(world.sides[side], own)
    before = _ledger(requester)
    response = _client(requester).post(url, data)
    label = f"POST {url} {data}"
    assert response.status_code in body.refused, f"{label} answered {response.status_code}, not {body.refused}"
    leaked = _leaks(response, forbidden)
    assert not leaked, f"{label} leaked {leaked}"
    changed = _changes(before, _ledger(requester))
    assert not changed, f"refused {label} changed {changed}"
    assert not (body.owner_check and body.owner_check(response)), f"{label} was accepted"


@pytest.mark.parametrize(
    "name, probe", [(name, probe) for name, route in OBJECT_ROUTES.items() for probe in route.body]
)
def test_owner_body_probe_changes_something(world, name, probe):
    """The owner's same POST on their own object gets past the form's other checks, so the probes reach the lookup."""
    body = OBJECT_ROUTES[name].body[probe]
    own = world.own["owner"]
    data = body.data(world.sides[body.sides[0]], own)
    before = _ledger(world.owner)
    response = _client(world.owner).post(reverse(name, kwargs=body.path(own)), data)
    label = f"the owner's POST {name} {data}"
    if body.owner_check:
        assert body.owner_check(response), f"{label} was not accepted"
    else:
        assert response.status_code == 302, f"{label} answered {response.status_code}"
        assert _changes(before, _ledger(world.owner)), f"{label} changed nothing"


def _says_why(reason):
    # A reason is a sentence about the body, so a placeholder such as "x" does not count.
    return len(reason.split()) >= 4


def test_every_object_route_that_takes_a_post_says_what_its_body_names(world):
    """An object route that accepts POST declares the ids its body names, or why it names none."""
    client = _client(world.owner)
    silent = []
    for name, route in OBJECT_ROUTES.items():
        if route.body or _says_why(route.body_reason):
            continue
        side = world.sides[(route.owner_sides or route.sides)[0]]
        if client.post(reverse(name, kwargs=route.kwargs(side))).status_code != 405:
            silent.append(name)
    assert not silent, f"object routes that accept POST without body or body_reason: {silent}"


def _probe_value(world, param):
    target = getattr(world.sides["private"], PROBE.get(param, "account"))
    return [str(item.pk) for item in target] if isinstance(target, list) else str(target.pk)


def _baseline(world, route):
    month_start = world.when.replace(day=1)
    values = {
        "date_from": month_start.isoformat(),
        "date_to": world.when.replace(day=28).isoformat(),
        "year": str(world.when.year),
        "month": month_start.strftime("%Y-%m"),
    }
    return {param: values[param] for param in route.baseline}


def _probes(world, route):
    """The baseline alone, then each scoping parameter on its own on top of it."""
    baseline = _baseline(world, route)
    yield baseline
    for param in route.query:
        values = ("private", "household") if param == "scope" else (_probe_value(world, param),)
        for value in values:
            yield {**baseline, param: value}


def _hidden_ids(requester):
    """Accounts the requester cannot see, the transactions and batches on them, and others' chats."""
    hidden = set(
        Account.objects.exclude(pk__in=Account.objects.visible_to(requester).values("pk")).values_list("pk", flat=True)
    )
    return {
        "account_id": hidden,
        "transaction_id": set(Transaction.objects.filter(account_id__in=hidden).values_list("pk", flat=True)),
        "import_batch_id": set(ImportBatch.objects.filter(account_id__in=hidden).values_list("pk", flat=True)),
        "conversation_id": set(AiConversation.objects.exclude(member=requester).values_list("pk", flat=True)),
    }


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
    for query in _probes(world, route):
        response = client.get(url, query)
        if route.form is not None and response.status_code == 405:
            break
        assert response.status_code in PAGE_ANSWERS, f"GET {url} {query} answered {response.status_code}"
        leaked = _leaks(response, forbidden)
        assert not leaked, f"GET {url} {query} leaked {leaked}"
    if route.form is None:
        return
    data = {**route.extra, **{param: _probe_value(world, param) for param in route.form}}
    hidden = _hidden_ids(requester)
    before = _ledger(requester)
    response = client.post(url, data)
    assert response.status_code in PAGE_POST_ANSWERS, f"POST {url} {data} answered {response.status_code}"
    leaked = _leaks(response, forbidden)
    assert not leaked, f"POST {url} {data} leaked {leaked}"
    after = _ledger(requester)
    for model, rows in before.items():
        kept = {row["id"]: row for row in after[model]}
        changed = [row["id"] for row in rows if kept.get(row["id"]) != row]
        assert not changed, f"POST {url} changed {model} rows {changed}"
        # The requester may add rows of their own, never one on an account they cannot see.
        existing = {row["id"] for row in rows}
        added = [row for row in after[model] if row["id"] not in existing]
        trespass = [
            row["id"] for row in added if any(row.get(column) in ids for column, ids in hidden.items())
        ]
        assert not trespass, f"POST {url} added {model} rows {trespass} on objects hidden from the {who}"


def _action_cases():
    """The member names the owner's private object; the outsider names either side's."""
    return [
        pytest.param(name, action, side, who, id=f"{name}-{action}-{side}-{who}")
        for name, route in PAGE_ROUTES.items()
        for action, form in route.actions.items()
        for who, sides in (("member", ("private",)), ("outsider", ("private", "shared")))
        for side in form.sides
        if side in sides
    ]


@pytest.mark.parametrize("name, action, side, who", _action_cases())
def test_page_actions_refuse_another_members_or_households_object(world, name, action, side, who):
    form = PAGE_ROUTES[name].actions[action]
    requester = getattr(world, who)
    forbidden = PRIVATE_FORBIDDEN if who == "member" else OUTSIDER_FORBIDDEN
    url = reverse(name)
    data = form.data(world.sides[side], world.own[who])
    before = _ledger(requester)
    response = _client(requester).post(url, data)
    label = f"POST {url} {data}"
    assert response.status_code in form.refused, f"{label} answered {response.status_code}, not {form.refused}"
    leaked = _leaks(response, forbidden)
    assert not leaked, f"{label} leaked {leaked}"
    changed = _changes(before, _ledger(requester))
    assert not changed, f"refused {label} changed {changed}"


@pytest.mark.parametrize(
    "name, action", [(name, action) for name, route in PAGE_ROUTES.items() for action in route.actions]
)
def test_owner_page_action_changes_something(world, name, action):
    """The owner's same POST gets past the form's other checks, so the probes reach the lookup."""
    form = PAGE_ROUTES[name].actions[action]
    data = form.data(world.sides[form.sides[0]], world.own["owner"])
    before = _ledger(world.owner)
    response = _client(world.owner).post(reverse(name), data)
    assert response.status_code == 302, f"the owner's POST {name} {data} answered {response.status_code}"
    assert _changes(before, _ledger(world.owner)), f"the owner's POST {name} {data} changed nothing"


def test_every_page_that_takes_a_post_says_what_it_names(world):
    """A page that accepts POST declares the ids its body names, or why it names none."""
    client = _client(world.owner)
    silent = [
        name
        for name, route in PAGE_ROUTES.items()
        # An empty form still posts, but declares no id, so it needs a reason too.
        if not route.form and not route.actions and not _says_why(route.post_reason)
        and client.post(reverse(name, kwargs=route.kwargs[0])).status_code != 405
    ]
    assert not silent, f"pages that accept POST without form, actions or post_reason: {silent}"


def _owner_canary_cases():
    return [name for name, route in PAGE_ROUTES.items() if route.owner_canary]


@pytest.mark.parametrize("name", _owner_canary_cases())
def test_owner_sees_the_canary_on_the_baseline_page(world, name):
    route = PAGE_ROUTES[name]
    query = _baseline(world, route)
    response = _client(world.owner).get(reverse(name, kwargs=route.kwargs[0]), query)
    assert response.status_code == 200, f"{name} {query} answered {response.status_code}"
    assert CANARY in _text(response), f"the owner does not see the canary on {name} {query}"


@pytest.mark.parametrize("name", [name for name, route in PAGE_ROUTES.items() if route.owner_post])
def test_owner_form_post_gets_past_the_other_checks(world, name):
    route = PAGE_ROUTES[name]
    data = {**route.extra, **{param: _probe_value(world, param) for param in route.form or ()}}
    response = _client(world.owner).post(reverse(name, kwargs=route.kwargs[0]), data)
    assert route.owner_post(world, response), f"the owner's POST {name} {data} did not reach its lookup"
