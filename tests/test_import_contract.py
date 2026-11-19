"""Import contract for finance/ (#755): no new private cross-module imports, and ratchets.

finance/ is one flat package, and nothing else checks how its modules depend on
each other. This scan parses every module with ast (it never imports finance or
Django settings) and checks three things:

1. Private cross-module imports. A private name is one that starts with "_" and
   is not a dunder ("__all__"). `from .x import _name`, `from ..x import _name`
   or `from finance.x import _name` in a module other than x is a private
   cross-module import, recorded as "importer -> x._name", with module names
   dotted relative to finance/ ("csv_import.services"; finance/__init__.py is
   "__init__"). Each imported name is one entry, wherever the import sits (top
   level or inside a function); the same name imported twice by one module is
   one entry. Importing a submodule (`from . import _x`) is not a private name.
   Every entry must be in PRIVATE_IMPORT_BASELINE, which may only shrink: a new
   entry fails, and so does a baselined entry the code no longer has, so the
   list is pruned as the code is fixed. Attribute access such as
   `module._name` is not scanned.

2. Imports inside functions. Every `import` or `from ... import` statement
   whose enclosing scope includes a function or method (at any depth) counts
   once, whatever it imports (finance, Django, the standard library) and however
   many names it lists. Statements at module or class level, including those in
   module-level `if` or `try` blocks, do not count. The count may not rise above
   MAX_FUNCTION_IMPORTS.

3. The largest import cycle. The graph has one node per module and an edge from
   a module to each finance module it imports, at any level, including imports
   inside functions. `from .x import y` is an edge to x.y when x.y is a module
   (a submodule import) and to x otherwise; `import finance.x` is an edge to x,
   and `import finance` an edge to "__init__". Relative imports resolve from the
   importer's package; a package is a directory with an __init__.py, even one
   with no other modules.
   Self-edges are dropped. A cycle is a strongly connected component (Tarjan's
   algorithm); its size is its module count. The largest may not grow above
   MAX_CYCLE_SIZE.

Both ratchets fail when the number rises and, when it falls, emit a warning
naming the new number so that PR lowers the recorded value. Raising a recorded
number, or adding a baseline entry, loosens a check: list it under the PR's
"### Gate changes" with the reason.

finance/migrations/ is excluded throughout: Django generates it and no app
module imports it.
"""

import ast
import functools
import warnings
from pathlib import Path

FINANCE = Path(__file__).resolve().parent.parent / "finance"
PACKAGE = "finance"
ROOT_MODULE = "__init__"

# Measured on main at a4fc0ac. Lower these as they fall; raising one is a gate change.
MAX_FUNCTION_IMPORTS = 205
MAX_CYCLE_SIZE = 65

# Today's private cross-module imports (main at a4fc0ac). Shrink only.
PRIVATE_IMPORT_BASELINE = frozenset({
    "access -> models._person_for",
    "ai_jobs -> ai_services._token",
    "ai_jobs -> monthly_review_ai._requeue_if_regenerated",
    "ai_lookup_tools -> ai_tools._account_ids",
    "ai_lookup_tools -> ai_tools._ai_scope",
    "ai_lookup_tools -> ai_tools._figure",
    "ai_lookup_tools -> ai_tools._parse_date",
    "ai_lookup_tools -> ai_tools._scope",
    "ai_lookup_tools -> ai_tools._visible_account",
    "ai_plan -> ai_harness._label",
    "ai_plan -> lifecycle_services._DENIED",
    "ai_plan -> lifecycle_services._person_for",
    "ai_services -> lifecycle_services._DENIED",
    "ai_services -> lifecycle_services._person_for",
    "alert_services -> models._person_for",
    "audit_models -> models._person_for",
    "audit_operations -> models._person_for",
    "audit_services -> models._person_for",
    "budget_services -> cash_flow._combine_category_spending",
    "bulk_edit_services -> models._person_for",
    "calculated_balances -> lifecycle_services._person_for",
    "calculated_balances -> reconciliation._coverage",
    "calculated_balances -> reconciliation._covered",
    "calculated_balances -> reconciliation._daily_totals",
    "calculated_balances -> snapshot_services._lock_editable_account",
    "calculated_balances -> snapshot_services._record_snapshot",
    "chat_attachments -> ai_jobs._connection_marker",
    "chat_attachments -> models._person_for",
    "chat_balance_cards -> chat_proposals._Stale",
    "chat_balance_cards -> chat_proposals._card_items",
    "chat_balance_cards -> chat_proposals._form_problem",
    "chat_balance_cards -> chat_proposals._int_or_none",
    "chat_balance_cards -> chat_proposals._items_schema",
    "chat_balance_cards -> chat_proposals._raw_items",
    "chat_balance_cards -> chat_proposals._store_items",
    "chat_balance_cards -> chat_proposals._text",
    "chat_balance_cards -> snapshot_services._lock_editable_account",
    "chat_charts -> ai_tools._account_ids",
    "chat_charts -> ai_tools._ai_scope",
    "chat_charts -> ai_tools._figure",
    "chat_charts -> ai_tools._range_query",
    "chat_charts -> ai_tools._transaction_scope",
    "chat_charts -> ai_tools._visible_account",
    "chat_cleanup_cards -> ai_tools._figure",
    "chat_cleanup_cards -> bulk_edit_services._apply_note",
    "chat_cleanup_cards -> chat_proposals._Stale",
    "chat_cleanup_cards -> chat_proposals._card_items",
    "chat_cleanup_cards -> chat_proposals._category_by_name",
    "chat_cleanup_cards -> chat_proposals._category_name",
    "chat_cleanup_cards -> chat_proposals._int_or_none",
    "chat_cleanup_cards -> chat_proposals._items_schema",
    "chat_cleanup_cards -> chat_proposals._raw_items",
    "chat_cleanup_cards -> chat_proposals._store_items",
    "chat_cleanup_cards -> chat_proposals._text",
    "chat_proposals -> bulk_edit_services._apply_tags",
    "chat_runner -> ai_jobs._lock_qs",
    "chat_services -> ai_jobs._connection_marker",
    "chat_services -> lifecycle_services._DENIED",
    "chat_services -> lifecycle_services._person_for",
    "csv_import.saved_mappings -> lifecycle_services._DENIED",
    "csv_import.services -> lifecycle_services._DENIED",
    "csv_import.services -> lifecycle_services._lock_visible_account_with_pair_and_refund_counterparts",
    "csv_import.services -> lifecycle_services._person_for",
    "csv_import.services -> lifecycle_services._visible_account_for_update",
    "fund_draw_downs -> recurring_services._in_confirmed_amount_band",
    "loan_asset_pairing -> models._person_for",
    "loan_asset_pairing -> snapshot_services._lock_editable_account",
    "manual_entry_services -> lifecycle_services._DENIED",
    "manual_entry_services -> lifecycle_services._person_for",
    "manual_entry_services -> lifecycle_services._visible_account_for_update",
    "monthly_review -> models._person_for",
    "net_worth_views -> account_views._active_visible_account",
    "net_worth_views -> account_views._service_or_404",
    "receipt_services -> models._person_for",
    "recurring_review -> models._person_for",
    "simplefin_services -> lifecycle_services._DENIED",
    "simplefin_services -> lifecycle_services._person_for",
    "snapshot_services -> lifecycle_services._DENIED",
    "snapshot_services -> lifecycle_services._person_for",
    "spending_trends -> cash_flow._change_from_previous",
    "spending_trends -> cash_flow._combine_category_spending",
    "spending_trends -> cash_flow._filter_query",
    "tag_services -> models._person_for",
    "transaction_filters -> models._person_for",
    "unusual_spending -> models._person_for",
    "unusual_spending_ai -> monthly_review_ai._run_review_phrasing_job",
    "weekly_digest -> models._person_for",
})


def finance_sources(root=FINANCE):
    """Return ({dotted module name: source}, {names that are packages}), relative to finance/."""
    sources = {}
    packages = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] == "migrations":
            continue
        parts = list(relative.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        name = ".".join(parts) or ROOT_MODULE
        sources[name] = path.read_text(encoding="utf-8")
        if is_package:
            packages.add(name)
    return sources, packages


def _is_private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _package_parts(module, packages):
    """The package a module's relative imports start from, as a list of name parts."""
    if module == ROOT_MODULE:
        return []
    parts = module.split(".")
    return parts if module in packages else parts[:-1]


def _import_base(node, module, packages):
    """The dotted finance module a `from ... import` names, or None if it is outside finance."""
    if node.level == 0:
        name = node.module or ""
        if name == PACKAGE:
            return ""
        if name.startswith(PACKAGE + "."):
            return name[len(PACKAGE) + 1:]
        return None
    parts = _package_parts(module, packages)
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    if node.module:
        parts = parts + node.module.split(".")
    return ".".join(parts)


def _known(name, sources):
    name = name or ROOT_MODULE
    return name if name in sources else None


STATEMENT_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")


def _import_statements(tree):
    """Yield (statement, inside a function) for every import statement in a module.

    Import statements can only sit in statement blocks, so the walk skips
    expressions; that keeps the whole scan well under a second.
    """
    pending = [(tree, False)]
    while pending:
        node, in_function = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, in_function
            continue
        inner = in_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for field in STATEMENT_BLOCKS:
            pending.extend((child, inner) for child in getattr(node, field, ()))


def scan(sources, packages=frozenset()):
    """Return (private imports, function-import count, import graph).

    sources maps each module's dotted name to its source; packages names the
    modules that are packages (an __init__.py), which relative imports start from.
    """
    private = set()
    function_imports = 0
    graph = {module: set() for module in sources}
    for module, source in sources.items():
        for node, in_function in _import_statements(ast.parse(source)):
            function_imports += in_function
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                        target = _known(alias.name[len(PACKAGE) + 1:], sources)
                        if target:
                            graph[module].add(target)
            elif isinstance(node, ast.ImportFrom):
                base = _import_base(node, module, packages)
                if base is None:
                    continue
                for alias in node.names:
                    submodule = f"{base}.{alias.name}" if base else alias.name
                    if submodule in sources:
                        graph[module].add(submodule)
                        continue
                    target = _known(base, sources)
                    if target is None:
                        continue
                    graph[module].add(target)
                    if target != module and _is_private(alias.name):
                        private.add(f"{module} -> {target}.{alias.name}")
        graph[module].discard(module)
    return private, function_imports, graph


def strongly_connected_components(graph):
    """Tarjan's algorithm, iterative so a deep import chain cannot hit the recursion limit."""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    components = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph[child]))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                components.append(component)
    return components


def largest_cycle(graph):
    """The largest strongly connected component with more than one module, or an empty set."""
    cycles = [component for component in strongly_connected_components(graph) if len(component) > 1]
    return max(cycles, key=len, default=set())


def _ratchet(name, found, recorded, detail=""):
    assert found <= recorded, (
        f"{name} rose from {recorded} to {found}.{detail} Remove the new one, or raise the recorded "
        "value with a reason under the PR's ### Gate changes."
    )
    if found < recorded:
        message = f"{name} fell from {recorded} to {found}: lower the recorded value in {Path(__file__).name}."
        print(message)
        warnings.warn(message, stacklevel=2)


@functools.cache
def finance_scan():
    return scan(*finance_sources())


def test_no_new_private_cross_module_imports():
    private, _, _ = finance_scan()
    new = sorted(private - PRIVATE_IMPORT_BASELINE)
    stale = sorted(PRIVATE_IMPORT_BASELINE - private)
    assert not new, (
        "New private cross-module imports. Import a public name from the module that owns it, or give "
        "the helper a public name there:\n  " + "\n  ".join(new)
    )
    assert not stale, (
        "These baseline entries no longer exist; delete them from PRIVATE_IMPORT_BASELINE:\n  "
        + "\n  ".join(stale)
    )


def test_imports_inside_functions_do_not_increase():
    _, function_imports, _ = finance_scan()
    _ratchet("Imports inside functions", function_imports, MAX_FUNCTION_IMPORTS)


def test_largest_import_cycle_does_not_grow():
    _, _, graph = finance_scan()
    cycle = largest_cycle(graph)
    _ratchet("The largest import cycle", len(cycle), MAX_CYCLE_SIZE, f" Its modules: {', '.join(sorted(cycle))}.")


# The scanner itself, on synthetic modules, so the counting rules above hold as written.


def test_scan_resolves_relative_absolute_and_submodule_imports():
    sources = {
        ROOT_MODULE: "",
        "models": "def _person_for(p): return None\n",
        "access": "from .models import _person_for, Account\nfrom finance.models import __all__\n",
        "csv_import": "",
        "csv_import.services": "from ..models import _helper\nfrom . import _private_submodule\n",
        "csv_import._private_submodule": "from finance.csv_import.services import _x\n",
    }
    private, _, graph = scan(sources, packages={ROOT_MODULE, "csv_import"})
    assert private == {
        "access -> models._person_for",
        "csv_import.services -> models._helper",
        "csv_import._private_submodule -> csv_import.services._x",
    }
    assert graph["csv_import.services"] == {"models", "csv_import._private_submodule"}


def test_scan_counts_each_import_statement_inside_a_function():
    sources = {
        "a": (
            "import os\n"
            "try:\n    import json\nexcept ImportError:\n    pass\n"
            "class C:\n    import re\n    def m(self):\n        from . import b\n"
            "def f():\n    import os, sys\n    def g():\n        from .b import _y\n"
        ),
        "b": "",
    }
    private, function_imports, _ = scan(sources)
    assert function_imports == 3
    assert private == {"a -> b._y"}


def test_largest_cycle_counts_imports_inside_functions():
    sources = {
        "a": "from .b import x\n",
        "b": "def f():\n    from .c import y\n",
        "c": "from . import a\n",
        "d": "from .a import z\n",
        "e": "from .e import w\n",
    }
    _, _, graph = scan(sources)
    assert largest_cycle(graph) == {"a", "b", "c"}


def test_package_with_only_an_initializer_resolves_relative_imports_from_itself():
    sources = {
        ROOT_MODULE: "",
        "parser": "",
        "csv_import": "",
        "csv_import.parser": "",
        "csv_import.helpers": "from ..parser import _parse_date\n",
    }
    private, _, _ = scan(sources, packages={ROOT_MODULE, "csv_import", "csv_import.helpers"})
    assert private == {"csv_import.helpers -> csv_import.parser._parse_date"}


def test_plain_import_of_the_package_is_an_edge_to_its_initializer():
    sources = {ROOT_MODULE: "from . import forms\n", "forms": "import finance\n"}
    _, _, graph = scan(sources, packages={ROOT_MODULE})
    assert largest_cycle(graph) == {ROOT_MODULE, "forms"}
