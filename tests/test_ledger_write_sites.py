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
    "simplefin_services.py:_upsert_snapshot": "simplefin_services.py:_sync_one_link",
    "simplefin_services.py:_import_transactions": "simplefin_services.py:_sync_one_link",
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


def _ledger_variables(function, helpers):
    """Local names bound to something built from a ledger model or a module helper that reads one."""
    names = {arg.arg for arg in function.args.args if arg.arg in {"snapshot", "transaction"}}
    while True:  # to a fixed point: `rows = helper()` then `for row in rows` binds `row` too
        sources = LEDGER_MODELS | helpers | names
        found = set(names)
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and _names_in(node.value) & (LEDGER_MODELS | helpers):
                found |= {target.id for target in node.targets if isinstance(target, ast.Name)}
            elif isinstance(node, ast.For) and _names_in(node.iter) & sources and isinstance(node.target, ast.Name):
                found.add(node.target.id)
        if found == names:
            return names
        names = found


def _is_ledger_write(call, ledger_variables):
    if not isinstance(call.func, ast.Attribute):
        return False
    method, receiver = call.func.attr, call.func.value
    if method in QUERYSET_WRITES and _root_name(receiver) in LEDGER_MODELS:
        if method == "update":
            return bool({keyword.arg for keyword in call.keywords} & LEDGER_FIELDS)
        if method == "bulk_update":
            return bool(_constant_strings(call.args[1] if len(call.args) > 1 else call) & LEDGER_FIELDS)
        return True
    if isinstance(receiver, ast.Name) and receiver.id in ledger_variables:
        if method == "delete":
            return True
        if method == "save":
            fields = [keyword.value for keyword in call.keywords if keyword.arg == "update_fields"]
            return not fields or bool(_constant_strings(fields[0]) & LEDGER_FIELDS)
    return False


def _calls_refresh(function):
    return any(
        isinstance(node, ast.Call) and _root_name(node.func) in REFRESH_CALLS
        or isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in REFRESH_CALLS
        for node in ast.walk(function)
    )


def ledger_write_sites():
    """{"path/under/finance.py:function": calls_refresh} for every function in finance/ that writes ledger rows."""
    sites = {}
    for path in sorted(FINANCE.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = _ledger_helpers(tree)
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            variables = _ledger_variables(function, helpers)
            if any(isinstance(node, ast.Call) and _is_ledger_write(node, variables) for node in ast.walk(function)):
                sites[f"{path.relative_to(FINANCE).as_posix()}:{function.name}"] = _calls_refresh(function)
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
