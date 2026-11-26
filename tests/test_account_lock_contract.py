"""Every Account row lock in finance/ goes through finance.account_locks.lock_accounts (#810).

Owner decision 2026-11-25 (review r3 on #811): one helper takes account row
locks, in pk order, and holds the member-deletion held-set guard. This test
reads the source and fails on any other `select_for_update` that can lock an
Account row:

- an Account queryset, however the model is named (an import alias,
  `models.Account`, `visible_rows(Account, ...)`, a queryset variable built
  earlier in the function);
- a lock whose `of=` targets do not exclude a join to accounts (no `of`, or
  `of=()`, locks every joined table; PostgreSQL then locks the account rows
  too). Joins are read from `select_related`, filter keys, `Q`, `F` and
  aggregate paths, and ordering or values fields, through Django's model
  metadata, including querysets built earlier in the function;
- a lock with a join or expression the scan cannot read (an opaque filter
  expression, a bare `select_related()`, a computed `of`), unless `of`
  restricts it to rows that are not accounts;
- a lock on a receiver whose model the scan cannot establish (a parameter, a
  helper's queryset, `model.objects` in a loop). It fails closed: such sites
  are listed in GENERIC_LOCKS with the reason they never lock an Account, and
  a new one fails until it is reviewed and listed.

Static only; no database. Concurrency review r1 at ba14aa3 (#811 comment
7173541959) added the alias, `of=()` and filter-join shapes.
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
    ("finance/break_glass.py", "locked_grant_for_redeeming"): "BreakGlassGrant rows, of=self (joins the user model only)",
    ("finance/chat/cleanup_cards.py", "_locked"): "RecurringSeries, TransferPair and CategorySuggestion rows, of=self",
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
    """The calls of a method chain, outermost first, the attribute names after its root, and the root."""
    calls, attributes = [], []
    while True:
        if isinstance(node, ast.Call):
            calls.append(node)
            node = node.func
        elif isinstance(node, ast.Attribute):
            attributes.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            attributes.reverse()
            return calls, attributes, node


def _models():
    from django.apps import apps
    from django.contrib.auth import get_user_model

    by_name = {model.__name__: model for model in apps.get_models()}
    by_name.update({model.__name__: model for model in apps.get_app_config("finance").get_models()})
    user = get_user_model()
    by_name.setdefault(user.__name__, user)
    return by_name, apps.get_model("finance", "Account"), user


class _Module:
    """What one module's imports say about names: model aliases and module aliases."""

    def __init__(self, tree):
        self.models, self.account, self.user = _models()
        self.aliases = {}
        self.modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name in self.models:
                        self.aliases[local] = alias.name
                    elif alias.name == "models":
                        self.modules.add(local)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(".models") or alias.name == "finance.models":
                        self.modules.add(alias.asname or alias.name.split(".")[0])

    def model_named(self, node):
        """The model an expression names (a class, an alias, models.X, get_user_model()), or None."""
        if isinstance(node, ast.Name):
            name = self.aliases.get(node.id, node.id)
            return self.models.get(name)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.modules:
            return self.models.get(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_user_model":
            return self.user
        return None


def _local_assignments(function):
    assigned = {}
    if function is None:
        return assigned
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned.setdefault(node.targets[0].id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assigned.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            assigned.setdefault(node.target.id, []).append(node.value)  # condition |= Q(...)
    return assigned


def _receiver(module, node, assigned, depth=0):
    """(model or None, every call of the chain and of the querysets it was built from)."""
    calls, attributes, root = _chain(node)
    for call in calls:
        if isinstance(call.func, ast.Name) and call.func.id == "visible_rows" and call.args:
            return module.model_named(call.args[0]), calls
    model = module.model_named(root)
    if model is None and isinstance(root, ast.Name) and root.id in module.modules and attributes:
        model = module.models.get(attributes[0])
    if model is not None:
        return model, calls
    if isinstance(root, ast.Name) and root.id in assigned and depth < 5:
        models, gathered = set(), list(calls)
        for value in assigned[root.id]:
            value_calls, _attributes, value_root = _chain(value)
            if isinstance(value_root, ast.Name) and value_root.id == root.id:
                gathered += value_calls  # query = query.filter(...): only adds calls
                continue
            origin, origin_calls = _receiver(module, value, assigned, depth + 1)
            models.add(origin)
            gathered += origin_calls
        if len(models) == 1 and None not in models:
            return models.pop(), gathered
    return None, calls


PATH_METHODS = {"filter", "exclude", "get", "order_by", "values", "values_list", "only", "defer", "distinct",
                "annotate", "alias", "get_or_create", "update_or_create"}
PATH_CALLS = {"F", "Q", "Count", "Sum", "Min", "Max", "Avg", "OuterRef"}


def _paths(calls, assigned=None):
    """(select_related paths, filter-style paths, whether some expression could not be read).

    A name in a path position (a positional filter argument, an annotation, a
    Q's positional argument) is followed to what the enclosing function
    assigned to it; one it cannot follow (a parameter, a global) makes the
    query unreadable, so the lock fails closed (review r6 F2 on #811). A name
    used as a filter value is followed too, but a parameter there is a plain
    value.
    """
    assigned = assigned or {}
    selected, filtered, opaque = [], [], False
    following = set()

    def follow(name, visit):
        if name in following:
            return True
        following.add(name)
        try:
            for assigned_value in assigned.get(name, ()):
                visit(assigned_value)
        finally:
            following.discard(name)
        return name in assigned

    def value(node):
        """A filter value: only F, Q and aggregate calls inside it name paths; anything else is compared against."""
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id in PATH_CALLS:
                expression(inner)
            elif isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                follow(inner.id, value)

    def expression(node):
        nonlocal opaque
        if isinstance(node, ast.Name):
            if not follow(node.id, expression):
                opaque = True
            return
        if isinstance(node, ast.Constant):
            return
        if isinstance(node, ast.BinOp):
            expression(node.left)
            expression(node.right)
        elif isinstance(node, ast.UnaryOp):
            expression(node.operand)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in PATH_CALLS:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    filtered.append(arg.value)
                else:
                    expression(arg)
            for keyword in node.keywords:
                if keyword.arg is None:
                    opaque = True
                else:
                    filtered.append(keyword.arg)
                    value(keyword.value)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for element in node.elts:
                expression(element)
        elif isinstance(node, ast.Attribute):
            opaque = True  # self.condition, module.CONDITION: not followed
        else:
            opaque = True

    for call in calls:
        if not isinstance(call.func, ast.Attribute):
            continue
        method = call.func.attr
        if method == "select_related":
            if not call.args:
                opaque = True
            for arg in call.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    selected.append(arg.value)
                else:
                    opaque = True
        elif method in PATH_METHODS:
            for arg in call.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    filtered.append(arg.value.lstrip("-"))
                elif isinstance(arg, ast.Starred):
                    opaque = True
                else:
                    expression(arg)
            for keyword in call.keywords:
                if keyword.arg is None:
                    opaque = True
                elif method in ("annotate", "alias"):
                    expression(keyword.value)
                elif keyword.arg in ("defaults", "create_defaults"):
                    continue
                else:
                    filtered.append(keyword.arg)
                    value(keyword.value)
    return selected, filtered, opaque


def _reaches_account(model, path, account, *, select):
    """Whether following `path` from `model` joins the account table."""
    from django.core.exceptions import FieldDoesNotExist

    parts = path.split("__")
    current = model
    for index, part in enumerate(parts):
        if part == "pk":
            return False
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return False  # a lookup or transform: the path ends here
        if not field.is_relation or field.related_model is None:
            return False
        target = field.related_model
        if target is account:
            if select or not (field.many_to_one or field.one_to_one) or not field.concrete:
                return True
            rest = parts[index + 1:]
            if not rest:
                return False  # account=..., compared on the local column
            try:
                return account._meta.get_field(rest[0]) != account._meta.pk
            except FieldDoesNotExist:
                return False  # account__in=..., a lookup on the local column
        current = target
    return False


def _of_targets(lock):
    """None when `of` locks every joined table (absent or empty), else its paths, or False if unreadable."""
    for keyword in lock.keywords:
        if keyword.arg != "of":
            continue
        value = keyword.value
        if isinstance(value, (ast.Tuple, ast.List)) and all(
            isinstance(element, ast.Constant) and isinstance(element.value, str) for element in value.elts
        ):
            targets = [element.value for element in value.elts]
            return targets or None
        if isinstance(value, ast.IfExp):
            return False
        return False
    return None


def _scan_source(relative, source):
    """Yield (module, function, line, kind) for each select_for_update in one module's source that may lock an Account.

    kind is "account" (an Account queryset), "joined account" (a join to
    accounts that `of` does not exclude), "possible joined account" (a join
    or expression this scan cannot read, without `of` restricting the lock),
    or "generic" (a receiver whose model the scan cannot establish).
    """
    tree = ast.parse(source)
    module = _Module(tree)
    enclosing = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(function):
                enclosing[id(inner)] = function
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        calls, _attributes, _root = _chain(node)
        locks = [call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == "select_for_update"]
        if not locks or id(locks[0]) in seen:
            continue
        seen.add(id(locks[0]))
        lock = locks[0]
        function = enclosing.get(id(lock))
        where = (relative, function.name if function is not None else "<module>", lock.lineno)
        assigned = _local_assignments(function)
        model, all_calls = _receiver(module, node, assigned)
        if model is None:
            yield (*where, "generic")
            continue
        if model is module.account:
            yield (*where, "account")
            continue
        targets = _of_targets(lock)
        if targets is False:
            yield (*where, "possible joined account")
            continue
        if targets is not None:
            if any(target != "self" and _reaches_account(model, target, module.account, select=True)
                   for target in targets):
                yield (*where, "joined account")
            continue
        selected, filtered, opaque = _paths(all_calls, assigned)
        if any(_reaches_account(model, path, module.account, select=True) for path in selected) or any(
            _reaches_account(model, path, module.account, select=False) for path in filtered
        ):
            yield (*where, "joined account")
        elif opaque:
            yield (*where, "possible joined account")


def _locks():
    """Yield (module, function, line, kind) for every select_for_update that may lock an Account outside the helper."""
    for path in sorted((ROOT / "finance").rglob("*.py")):
        relative = path.relative_to(ROOT)
        if "migrations" in relative.parts or relative == HELPER:
            continue
        yield from _scan_source(relative.as_posix(), path.read_text(encoding="utf-8"))


WRITE_METHODS = ("update", "delete", "bulk_update")


def _account_accessors(account):
    """Reverse accessor names on other models that return Account querysets (household.accounts, ...)."""
    names = set()
    for field in account._meta.get_fields():
        if field.is_relation and field.many_to_one and field.concrete and field.remote_field is not None:
            names.add(field.remote_field.get_accessor_name())
    return names


def _scan_writes(relative, source):
    """Yield (module, function, line, method) for each Account queryset write in one module's source.

    Owner decision on review r6 of #811: an UPDATE or DELETE on accounts takes
    their row locks too, so it goes through finance.account_locks.update_accounts
    or delete_accounts and the member-deletion guard. A write counts when its
    receiver resolves to Account (as for locks) or goes through a reverse
    accessor that returns accounts (`household.accounts.update(...)`).
    """
    tree = ast.parse(source)
    module = _Module(tree)
    accessors = _account_accessors(module.account)
    enclosing = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(function):
                enclosing[id(inner)] = function
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS):
            continue
        function = enclosing.get(id(node))
        model, _calls = _receiver(module, node.func.value, _local_assignments(function))
        _calls, attributes, _root = _chain(node.func.value)
        if model is module.account or (model is None and accessors & set(attributes)):
            yield relative, function.name if function is not None else "<module>", node.lineno, node.func.attr


def _writes():
    for path in sorted((ROOT / "finance").rglob("*.py")):
        relative = path.relative_to(ROOT)
        if "migrations" in relative.parts or relative == HELPER:
            continue
        yield from _scan_writes(relative.as_posix(), path.read_text(encoding="utf-8"))


def test_account_rows_are_written_only_through_the_guarded_helpers():
    offending = [f"{module}:{line} in {function} ({method})" for module, function, line, method in _writes()]
    assert offending == [], (
        "Write Account rows with finance.account_locks.update_accounts or delete_accounts:\n" + "\n".join(offending)
    )


WRITE_CASES = {
    "Account.objects.filter(pk=1).update(name='x')": ["update"],
    "BankAccount.objects.filter(owner=person).delete()": ["delete"],
    "household.accounts.update(scope='private')": ["update"],
    "Account.objects.bulk_update(rows, ['name'])": ["bulk_update"],
    "Transaction.objects.filter(account_id=1).update(note='x')": [],
    "row.delete()": [],
}


@pytest.mark.parametrize("expression", sorted(WRITE_CASES))
def test_the_write_scanner_classifies_each_shape(expression):
    source = (
        "from finance.models import Account, Transaction\n"
        "from finance.models import Account as BankAccount\n"
        "def write(person, household, rows, row):\n    return " + expression + "\n"
    )
    found = [method for _module, _function, _line, method in _scan_writes("finance/new_service.py", source)]
    assert found == WRITE_CASES[expression]


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


SCANNER_CASES = {
    "Account.objects.select_for_update().filter(pk=1)": "account",
    "visible_rows(Account, person).select_for_update().first()": "account",
    "(Account.objects\n        .visible_to(person)\n        .select_for_update()\n        .first())": "account",
    "models.Account.objects.select_for_update().first()": "account",
    "Transaction.objects.select_for_update().select_related('account').first()": "joined account",
    "Transaction.objects.select_for_update(of=('self', 'account')).select_related('account').first()":
        "joined account",
    "Transaction.objects.select_for_update().filter(Q(account__household_id=1)).first()": "joined account",
    "Transaction.objects.select_for_update().order_by('account__name').first()": "joined account",
    "Transaction.objects.select_for_update().filter(amount_minor=F('account__minimum_payment_minor')).first()":
        "joined account",
    "Transaction.objects.select_for_update().filter(exclusion_exists_for(person)).first()":
        "possible joined account",
    "Transaction.objects.select_for_update(of=OF).first()": "possible joined account",
    "Transaction.objects.select_for_update().select_related().first()": "possible joined account",
    "qs.select_for_update()": "generic",
    "Transaction.objects.filter(condition).select_for_update().first()": "possible joined account",
    "Unknown.objects.select_for_update()": "generic",
}
ALLOWED_SHAPES = (
    "Budget.objects.select_for_update().filter(pk__in=sorted(ids), household=current_household(person)).first()",
    "Transaction.objects.select_for_update(of=('self',)).select_related('account').first()",
    "Transaction.objects.select_for_update().filter(account=account, account_id=1, account__in=[1]).first()",
    "Transaction.objects.select_for_update().filter(account__pk=1, account__id=2).first()",
    "Budget.objects.select_for_update().filter(category__name='x').first()",
)
PREAMBLE = "from finance import models\nfrom finance.models import Account, Budget, Transaction\n"


def _kinds(expression):
    source = PREAMBLE + "def lock(condition):\n    return " + expression + "\n"
    return [kind for _module, _function, _line, kind in _scan_source("finance/new_service.py", source)]


@pytest.mark.parametrize("expression", sorted(SCANNER_CASES))
def test_the_scanner_classifies_each_lock_shape(expression):
    assert _kinds(expression) == [SCANNER_CASES[expression]]


@pytest.mark.parametrize("expression", ALLOWED_SHAPES)
def test_the_scanner_accepts_locks_that_cannot_reach_an_account(expression):
    assert _kinds(expression) == []


def test_the_scanner_resolves_a_predicate_held_in_a_variable():
    """Review r6 F2 on #811: a Q built earlier in the function is a join source, not a value."""
    source = PREAMBLE + (
        "from django.db.models import Q\n"
        "def lock():\n"
        "    condition = Q(account__owner_id=1)\n"
        "    return Transaction.objects.filter(condition).select_for_update().first()\n"
        "def widened():\n"
        "    condition = Q(status='active')\n"
        "    condition |= Q(account__household_id=2)\n"
        "    return Transaction.objects.select_for_update().filter(condition).first()\n"
        "def safe():\n"
        "    condition = Q(category__name='x')\n"
        "    return Budget.objects.select_for_update().filter(condition).first()\n"
        "def given(condition):\n"
        "    return Budget.objects.select_for_update().filter(condition).first()\n"
    )
    found = {function: kind for _module, function, _line, kind in _scan_source("finance/new_service.py", source)}
    assert found == {"lock": "joined account", "widened": "joined account", "given": "possible joined account"}


def test_the_scanner_follows_a_queryset_built_earlier_in_the_function():
    source = PREAMBLE + (
        "def lock(person):\n"
        "    query = Transaction.objects.filter(account__owner=person)\n"
        "    query = query.select_for_update()\n"
        "    return query.first()\n"
        "def safe():\n"
        "    query = Budget.objects.filter(status='active')\n"
        "    return query.select_for_update().first()\n"
        "def unknown(rows):\n"
        "    query = rows if rows else Budget.objects.all()\n"
        "    return query.select_for_update().first()\n"
    )
    found = {function: kind for _module, function, _line, kind in _scan_source("finance/new_service.py", source)}
    assert found == {"lock": "joined account", "unknown": "generic"}


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
