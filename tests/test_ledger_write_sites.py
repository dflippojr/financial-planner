"""Class-level check for #676 (escaped-defect): every ledger write site recalculates.

#636 shipped saved calculated balances, and later writes to their inputs went
unnoticed because nothing tied each write path to the recalculation (#641,
#676). This scan finds every function in finance/ that writes a transaction's
existence, amount, date, account or status, or a balance row, and fails unless
that function calls refresh_calculated_balances (directly or through a
_refresh_calculated wrapper), is covered by a named caller that does, or is
exempt with a reason. A new write path fails here until it is wired up or
explicitly justified. The behaviour of each wired path is tested in
tests/test_calculated_balance_recalc.py and tests/properties.
"""

import ast
from pathlib import Path

FINANCE = Path(__file__).resolve().parent.parent / "finance"
LEDGER_MODELS = {"Transaction", "BalanceSnapshot"}
QUERYSET_WRITES = {"create", "bulk_create", "update_or_create", "get_or_create", "update", "delete", "bulk_update"}
LEDGER_FIELDS = {"status", "archived_at", "amount_minor", "transaction_date", "account", "account_id", "snapshot_date"}
REFRESH_CALLS = {"refresh_calculated_balances", "refresh_locked_calculated_balances", "_refresh_calculated"}

COVERED_BY_CALLER = {
    # The sync refreshes each account in its own transaction after it commits (see _refresh_calculated).
    "simplefin_services.py:_upsert_snapshot": "simplefin_services.py:sync_connection",
    "simplefin_services.py:_import_transactions": "simplefin_services.py:sync_connection",
    "snapshot_services.py:_apply_manual_fields": "snapshot_services.py:update_manual_snapshot",
}
EXEMPT = {
    "calculated_balances.py:_replace_calculated_rows": "writes the calculated rows themselves",
    "calculated_balances.py:delete_calculated_balances": "the member's own Delete of calculated rows",
    "lifecycle_services.py:archive_account": (
        "an archived account keeps its history, real and calculated balances alike, through its archive date"
    ),
    "lifecycle_services.py:_repair_then_delete_account_rows": "the account and every balance row go with it",
}


QUERYSET_CHAIN = {"objects", "filter", "exclude", "all", "select_for_update", "order_by", "only", "defer",
                  "select_related", "prefetch_related", "annotate", "distinct", "visible_to", "active"}


def _ledger_root(node):
    """The name a write's receiver is built on, following only manager and queryset methods.

    `rows.filter(...).update()` is rooted at `rows`; `txn.splits.all().delete()`
    is not rooted at `txn`, because it writes a related model's rows.
    """
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute) and node.attr in QUERYSET_CHAIN:
            node = node.value
        else:
            return node.id if isinstance(node, ast.Name) else None


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _names_in(node):
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _constant_strings(node):
    return {sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant) and isinstance(sub.value, str)}


def _ledger_helpers(tree):
    """Module functions that read ledger models, so a row they return is a ledger row."""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _names_in(node) & LEDGER_MODELS
    }


def _rooted_in(expr, sources):
    """Whether expr is built on one of `sources`: Model.objects..., helper(...), or list(...) of either."""
    while isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in {"list", "sorted", "tuple"}:
        if not expr.args:
            return False
        expr = expr.args[0]
    return _root_name(expr) in sources


def _ledger_variables(function, helpers):
    """Local names bound to ledger rows or querysets: from a ledger model, or a module helper that reads one."""
    names = {arg.arg for arg in function.args.args if arg.arg in {"snapshot", "transaction"}}
    while True:  # to a fixed point: `rows = helper()` then `for row in rows` binds `row` too
        sources = LEDGER_MODELS | helpers | names
        found = set(names)
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and _rooted_in(node.value, sources):
                found |= {target.id for target in node.targets if isinstance(target, ast.Name)}
            elif isinstance(node, ast.For) and _names_in(node.iter) & sources and isinstance(node.target, ast.Name):
                found.add(node.target.id)
        if found == names:
            return names
        names = found


def _is_ledger_write(call, ledger_variables):
    """A write to ledger rows, on the model's manager or through a variable holding its rows or queryset."""
    if not isinstance(call.func, ast.Attribute):
        return False
    method, receiver = call.func.attr, call.func.value
    if _ledger_root(receiver) not in LEDGER_MODELS | ledger_variables:
        return False
    if method == "update":
        return bool({keyword.arg for keyword in call.keywords} & LEDGER_FIELDS)
    if method == "bulk_update":
        return bool(_constant_strings(call.args[1] if len(call.args) > 1 else call) & LEDGER_FIELDS)
    if method == "save":
        fields = [keyword.value for keyword in call.keywords if keyword.arg == "update_fields"]
        return not fields or bool(_constant_strings(fields[0]) & LEDGER_FIELDS)
    return method in QUERYSET_WRITES


def _calls_refresh(function):
    return any(
        isinstance(node, ast.Call) and _root_name(node.func) in REFRESH_CALLS
        or isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in REFRESH_CALLS
        for node in ast.walk(function)
    )


def write_sites_in(source, prefix):
    """{"prefix:function": calls_refresh} for each function in `source` that writes ledger rows."""
    tree = ast.parse(source)
    helpers = _ledger_helpers(tree)
    sites = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        variables = _ledger_variables(function, helpers)
        if any(isinstance(node, ast.Call) and _is_ledger_write(node, variables) for node in ast.walk(function)):
            sites[f"{prefix}:{function.name}"] = _calls_refresh(function)
    return sites


def ledger_write_sites():
    """{"path/under/finance.py:function": calls_refresh} for every function in finance/ that writes ledger rows."""
    sites = {}
    for path in sorted(FINANCE.rglob("*.py")):
        if "migrations" not in path.parts:
            sites.update(write_sites_in(path.read_text(encoding="utf-8"), path.relative_to(FINANCE).as_posix()))
    return sites


def _function(key):
    module, name = key.split(":")
    (function,) = [node for node in ast.walk(ast.parse((FINANCE / module).read_text(encoding="utf-8")))
                   if isinstance(node, ast.FunctionDef) and node.name == name]
    return function


def test_every_ledger_write_site_recalculates_saved_balances():
    sites = ledger_write_sites()
    unhandled = sorted(key for key, refreshes in sites.items()
                       if not refreshes and key not in COVERED_BY_CALLER and key not in EXEMPT)
    assert unhandled == [], (
        "These functions change transactions or balances without calling refresh_calculated_balances "
        "(#641, #676). Call it under the account lock, or add the function to COVERED_BY_CALLER or EXEMPT "
        "with a reason."
    )


def test_the_write_site_registry_has_no_stale_entries():
    sites = ledger_write_sites()
    assert sorted(set(COVERED_BY_CALLER) - set(sites)) == []
    assert sorted(set(EXEMPT) - set(sites)) == []
    for caller in COVERED_BY_CALLER.values():
        assert _calls_refresh(_function(caller)), caller


def test_the_scan_sees_the_paths_this_fix_wired_up():
    """Guards the scanner itself: if it stopped matching these, the check above would pass vacuously."""
    sites = ledger_write_sites()
    for key in (
        "csv_import/services.py:commit_csv_import",
        "csv_import/services.py:archive_batch",
        "manual_entry_services.py:_create_manual_entry",
        "forms.py:apply",
        "snapshot_services.py:record_manual_snapshot",
        "snapshot_services.py:delete_manual_snapshot",
    ):
        assert sites.get(key) is True, key
    for helper in ("simplefin_services.py:_upsert_snapshot", "simplefin_services.py:_import_transactions"):
        assert sites.get(helper) is False, helper


SHAPES = """
def queryset_variable(ids):
    rows = Transaction.objects.filter(pk__in=ids)
    rows.update(amount_minor=1)

def chained_variable(ids):
    rows = Transaction.objects.filter(pk__in=ids)
    rows.filter(status="active").update(status="archived")

def listed_rows(account):
    for row in list(BalanceSnapshot.objects.filter(account=account)):
        row.delete()

def instance_save(account):
    Transaction(account=account).save()

def wired(ids):
    Transaction.objects.filter(pk__in=ids).delete()
    refresh_calculated_balances(None, 1)

def note_only(ids):
    rows = Transaction.objects.filter(pk__in=ids)
    rows.update(note="x")

def related_rows(txn_id):
    transaction = Transaction.objects.get(pk=txn_id)
    transaction.splits.all().delete()

def other_model(ids):
    pairs = TransferPair.objects.filter(leg_a__in=Transaction.objects.filter(pk__in=ids))
    pairs.update(status="undone")
"""


def test_the_scan_catches_each_write_shape_and_ignores_non_ledger_writes():
    """Regression for the review of #733: a write through a queryset variable must be seen."""
    assert write_sites_in(SHAPES, "shapes") == {
        "shapes:queryset_variable": False,
        "shapes:chained_variable": False,
        "shapes:listed_rows": False,
        "shapes:instance_save": False,
        "shapes:wired": True,
    }
