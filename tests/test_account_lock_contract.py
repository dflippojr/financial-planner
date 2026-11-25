"""Every Account row lock in finance/ goes through finance.account_locks.lock_accounts (#810).

Owner decision 2026-11-25 (review r3 on #811): one helper takes account row
locks, in pk order, and holds the member-deletion held-set guard. This test
reads the source and fails on any other `select_for_update` that can lock an
Account row:

- a chain rooted at `Account` or `visible_rows(Account, ...)`;
- a lock without `of=` on a chain whose `select_related` joins an account
  (PostgreSQL then locks the joined account rows too);
- a lock on a receiver this test cannot attribute to a model (a variable, a
  helper's queryset, `model.objects` in a loop). Such sites are listed in
  GENERIC_LOCKS with the reason they never lock an Account; a new one fails
  until it is reviewed and listed.

Static only; no database.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = Path("finance/account_locks.py")

# (module, enclosing function): why its lock never reaches an Account row.
GENERIC_LOCKS = {
    ("finance/ai/jobs.py", "_lock_qs"): "AiJob querysets only",
    ("finance/auth_services.py", "accept_invitation"): "Invitation rows",
    ("finance/auth_services.py", "lock_first_member_creation"): "the user model",
    ("finance/chat/balance_cards.py", "_manual_on"): "BalanceSnapshot rows, of=self",
    ("finance/chat/cleanup_cards.py", "_locked"): "RecurringSeries, TransferPair and CategorySuggestion rows, of=self",
    ("finance/chat/proposals.py", "_budget_for"): "Budget rows",
    ("finance/chat/proposals.py", "_apply_planned_row"): "PlannedItem rows",
    ("finance/chat/proposals.py", "_apply_goal_row"): "SavingsGoal rows",
    ("finance/chat/services.py", "keep_conversation_if_visible"): "non-account used rows; accounts go through lock_accounts first",
    ("finance/google_auth.py", "lock_member_for_sign_in_change"): "the user model",
    ("finance/lifecycle_services.py", "end_current_membership"): "PlannedItem, Budget, SavingsGoal and CategoryRule rows",
    ("finance/passkey_services.py", "_passkey_for_assertion"): "Passkey rows",
    ("finance/refund_links.py", "reapply_refund_link_drops_for_account"): "TransferPair rows, of=self",
    ("finance/refund_links.py", "restore_refund_categories"): "Transaction rows, of=self",
}


def _chain(node):
    """The calls of a method chain, outermost first, and the expression it starts from."""
    calls = []
    while True:
        if isinstance(node, ast.Call):
            calls.append(node)
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            return calls, node


def _is_account_chain(calls, root):
    if isinstance(root, ast.Name) and root.id == "Account":
        return True
    return any(
        isinstance(call.func, ast.Name) and call.func.id == "visible_rows"
        and call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "Account"
        for call in calls
    )


def _joins_an_account(calls):
    for call in calls:
        if isinstance(call.func, ast.Attribute) and call.func.attr == "select_related":
            for arg in call.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    parts = arg.value.split("__")
                    if any(part in ("account", "linked_account", "secured_asset") for part in parts):
                        return True
    return False


def _is_model_root(root):
    if isinstance(root, ast.Name):
        return root.id[:1].isupper()
    return isinstance(root, ast.Call) and isinstance(root.func, ast.Name) and root.func.id == "get_user_model"


def _scan_source(relative, source):
    """Yield (module, function, line, kind) for each select_for_update in one module's source that may lock an Account."""
    tree = ast.parse(source)
    functions = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(function):
                functions[id(inner)] = function.name
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        calls, root = _chain(node)
        locks = [call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == "select_for_update"]
        if not locks or id(locks[0]) in seen:
            continue
        seen.add(id(locks[0]))
        lock = locks[0]
        where = (relative, functions.get(id(lock), "<module>"))
        if _is_account_chain(calls, root):
            yield (*where, lock.lineno, "account")
        elif _joins_an_account(calls) and not any(keyword.arg == "of" for keyword in lock.keywords):
            yield (*where, lock.lineno, "joined account")
        elif not _is_model_root(root) and not any(
            isinstance(call.func, ast.Name) and call.func.id == "visible_rows" for call in calls
        ):
            yield (*where, lock.lineno, "generic")


def _locks():
    """Yield (module, function, line, kind) for every select_for_update that may lock an Account outside the helper."""
    for path in sorted((ROOT / "finance").rglob("*.py")):
        relative = path.relative_to(ROOT)
        if "migrations" in relative.parts or relative == HELPER:
            continue
        yield from _scan_source(relative.as_posix(), path.read_text(encoding="utf-8"))


def test_account_rows_are_locked_only_through_lock_accounts():
    offending = [
        f"{module}:{line} in {function} ({kind})"
        for module, function, line, kind in _locks()
        if kind != "generic" or (module, function) not in GENERIC_LOCKS
    ]
    assert offending == [], (
        "Lock Account rows with finance.account_locks.lock_accounts; for a lock that never reaches an Account, "
        "add (module, function) to GENERIC_LOCKS with the reason:\n" + "\n".join(offending)
    )


def test_every_listed_generic_lock_still_exists():
    found = {(module, function) for module, function, _line, kind in _locks() if kind == "generic"}
    assert set(GENERIC_LOCKS) <= found, sorted(set(GENERIC_LOCKS) - found)


def test_the_helper_itself_locks_accounts():
    source = (ROOT / HELPER).read_text(encoding="utf-8") if (ROOT / HELPER).exists() else ""
    assert "select_for_update" in source and "_require_held_accounts" in source


def test_the_scanner_sees_each_kind_of_account_lock():
    """Guard the scanner against itself: each shape it must catch, on synthetic source."""
    samples = {
        "account": "Account.objects.select_for_update().filter(pk=1)",
        "visible": "visible_rows(Account, person).select_for_update().first()",
        "multiline": "(Account.objects\n    .visible_to(person)\n    .select_for_update()\n    .first())",
        "joined": "Transaction.objects.select_for_update().select_related('account').first()",
        "generic": "qs.select_for_update()",
    }
    kinds = {}
    for name, expression in samples.items():
        calls, root = _chain(ast.parse(expression, mode="eval").body)
        if _is_account_chain(calls, root):
            kinds[name] = "account"
        elif _joins_an_account(calls):
            kinds[name] = "joined account"
        elif not _is_model_root(root):
            kinds[name] = "generic"
    assert kinds == {"account": "account", "visible": "account", "multiline": "account",
                     "joined": "joined account", "generic": "generic"}
    assert not _joins_an_account(_chain(ast.parse("T.objects.select_related('category')", mode="eval").body)[0])


# Concurrency review r1 at ba14aa3 (#811 comment 7173541959): shapes the
# scanner must reject. Each is scanned as a new module in finance/.
REVIEW_SHAPES = {
    "aliased Account import": (
        "from finance.models import Account as BankAccount\n"
        "def lock():\n"
        "    return BankAccount.objects.select_for_update().filter(pk=1)\n"
    ),
    "empty of= with an account join": (
        "from finance.models import Transaction\n"
        "def lock():\n"
        "    return Transaction.objects.select_for_update(of=()).select_related('account').first()\n"
    ),
    "filter join to Account": (
        "from finance.models import Transaction\n"
        "def lock():\n"
        "    return Transaction.objects.select_for_update().filter(account__owner_id=1).first()\n"
    ),
}


@pytest.mark.parametrize("shape", sorted(REVIEW_SHAPES))
def test_the_review_shapes_are_rejected(shape):
    hits = list(_scan_source("finance/new_service.py", REVIEW_SHAPES[shape]))
    assert [kind for _module, _function, _line, kind in hits if kind != "generic"], (shape, hits)
