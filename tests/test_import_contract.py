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

4. Who imports the AI and chat packages (#771). finance/ai/ and finance/chat/
   hold the former ai_* and chat_* modules. A module outside them that imports
   either one, at any level and through the same graph as check 3, must be in
   AI_CHAT_IMPORTERS with exactly the AI and chat modules it imports today, and
   every entry there must still hold: a new importer or a new target fails, and
   so does an entry the code no longer needs. The old flat paths
   (finance/ai_services.py and the rest) are one-stage shims; no finance module
   may import one, so removing them later breaks nothing in finance/.

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

# Today's private cross-module imports (main at a4fc0ac; the ai_* and chat_* keys renamed to
# ai.* and chat.* when those modules moved into packages, #771). Shrink only.
PRIVATE_IMPORT_BASELINE = frozenset({
    "ai.jobs -> ai.services._token",
    "ai.jobs -> monthly_review_ai._requeue_if_regenerated",
    "ai.lookup_tools -> ai.tools._account_ids",
    "ai.lookup_tools -> ai.tools._ai_scope",
    "ai.lookup_tools -> ai.tools._figure",
    "ai.lookup_tools -> ai.tools._parse_date",
    "ai.lookup_tools -> ai.tools._scope",
    "ai.lookup_tools -> ai.tools._visible_account",
    "ai.plan -> ai.harness._label",
    "budget_services -> cash_flow._combine_category_spending",
    "calculated_balances -> reconciliation._coverage",
    "calculated_balances -> reconciliation._covered",
    "calculated_balances -> reconciliation._daily_totals",
    "calculated_balances -> snapshot_services._record_snapshot",
    "chat.attachments -> ai.jobs._connection_marker",
    "chat.balance_cards -> chat.proposals._Stale",
    "chat.balance_cards -> chat.proposals._card_items",
    "chat.balance_cards -> chat.proposals._form_problem",
    "chat.balance_cards -> chat.proposals._int_or_none",
    "chat.balance_cards -> chat.proposals._items_schema",
    "chat.balance_cards -> chat.proposals._raw_items",
    "chat.balance_cards -> chat.proposals._store_items",
    "chat.balance_cards -> chat.proposals._text",
    "chat.charts -> ai.tools._account_ids",
    "chat.charts -> ai.tools._ai_scope",
    "chat.charts -> ai.tools._figure",
    "chat.charts -> ai.tools._range_query",
    "chat.charts -> ai.tools._transaction_scope",
    "chat.charts -> ai.tools._visible_account",
    "chat.cleanup_cards -> ai.tools._figure",
    "chat.cleanup_cards -> chat.proposals._Stale",
    "chat.cleanup_cards -> chat.proposals._card_items",
    "chat.cleanup_cards -> chat.proposals._category_by_name",
    "chat.cleanup_cards -> chat.proposals._category_name",
    "chat.cleanup_cards -> chat.proposals._int_or_none",
    "chat.cleanup_cards -> chat.proposals._items_schema",
    "chat.cleanup_cards -> chat.proposals._raw_items",
    "chat.cleanup_cards -> chat.proposals._store_items",
    "chat.cleanup_cards -> chat.proposals._text",
    "chat.runner -> ai.jobs._lock_qs",
    "chat.services -> ai.jobs._connection_marker",
    "csv_import.services -> lifecycle_services._lock_visible_account_with_pair_and_refund_counterparts",
    "csv_import.services -> lifecycle_services._visible_account_for_update",
    "fund_draw_downs -> recurring_services._in_confirmed_amount_band",
    "manual_entry_services -> lifecycle_services._visible_account_for_update",
    "net_worth_views -> account_views._active_visible_account",
    "spending_trends -> cash_flow._change_from_previous",
    "spending_trends -> cash_flow._combine_category_spending",
    "spending_trends -> cash_flow._filter_query",
    "unusual_spending_ai -> monthly_review_ai._run_review_phrasing_job",
})


AI_CHAT_PACKAGES = ("ai", "chat")

# Every module outside finance/ai/ and finance/chat/ that imports them (main at 3134a44, #771):
# importer -> (the AI and chat modules it imports, why). Exact: a new importer or target fails,
# and so does an entry the code no longer has.
AI_CHAT_IMPORTERS = {
    "background": (
        {"ai.jobs", "chat.runner"},
        "The worker thread runs the AI job lane and the chat turn lane.",
    ),
    "budget_services": (
        {"chat.services"},
        "Making a household budget private purges conversations that quoted it (in a function).",
    ),
    "category_suggestion_services": (
        {"ai.jobs", "ai.services", "ai.tools", "ai.types"},
        "AI category suggestions queue and run as AI jobs.",
    ),
    "forms.ai": (
        {"ai.api", "ai.types"},
        "The AI settings forms list the API providers and their labels.",
    ),
    "lifecycle_services": (
        {"chat.services"},
        "Account, household and membership changes purge conversations that lost access (in functions).",
    ),
    "models": (
        {"ai.types"},
        "The models package re-exports the backend label helpers models.ai uses.",
    ),
    "models.ai": (
        {"ai.types"},
        "AI model rows show backend labels from ai.types; ai.types imports no models, so it stays one-way.",
    ),
    "monthly_review_ai": (
        {"ai.jobs", "ai.services", "ai.tools", "ai.types"},
        "The monthly review's AI phrasing runs as an AI job.",
    ),
    "planning_services": (
        {"chat.services"},
        "Making a planned item private purges conversations that quoted it (in a function).",
    ),
    "policy_services": (
        {"ai.services"},
        "The privacy policy page says whether the member has AI connected (in a function).",
    ),
    "savings_goal_services": (
        {"chat.services"},
        "Making a household savings goal private purges conversations that quoted it (in a function).",
    ),
    "settings_views": (
        {"ai.views"},
        "The settings page embeds the AI settings section.",
    ),
    "templatetags.ui": (
        {"ai.services", "ai.tiles", "chat.markdown", "chat.services"},
        "Shared page chrome: the chat bead and popover, AI tiles and the chat markdown filter.",
    ),
    "transaction_views": (
        {"ai.services"},
        "The transactions page offers AI category suggestions only to members with AI (in a function).",
    ),
    "unusual_spending_ai": (
        {"ai.jobs", "ai.services"},
        "Unusual spending's AI phrasing runs as an AI job.",
    ),
}


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


def _in_ai_chat(module):
    return module.split(".")[0] in AI_CHAT_PACKAGES


def _is_ai_chat_shim(module):
    """An old flat path such as ai_services: a top-level module that only re-exports a moved one."""
    return "." not in module and module.startswith(tuple(f"{package}_" for package in AI_CHAT_PACKAGES))


def ai_chat_importers(graph):
    """{module outside finance/ai/ and finance/chat/: the AI and chat modules it imports}.

    An import of an old shim path counts as an import of the package too, so a
    caller cannot bypass the allow-list through a shim.
    """
    found = {}
    for module, targets in graph.items():
        if _in_ai_chat(module) or _is_ai_chat_shim(module):
            continue
        hits = {target for target in targets if _in_ai_chat(target) or _is_ai_chat_shim(target)}
        if hits:
            found[module] = hits
    return found


def check_ai_chat_importers(graph, allowed):
    """Return the problems with graph's AI and chat importers against allowed, as messages."""
    found = ai_chat_importers(graph)
    problems = []
    for module in sorted(found.keys() - allowed.keys()):
        problems.append(f"{module} imports {', '.join(sorted(found[module]))} and is not in AI_CHAT_IMPORTERS")
    for module in sorted(found.keys() & allowed.keys()):
        targets, _ = allowed[module]
        if found[module] != targets:
            problems.append(
                f"{module} imports {', '.join(sorted(found[module]))}; AI_CHAT_IMPORTERS records "
                f"{', '.join(sorted(targets))}"
            )
    for module in sorted(allowed.keys() - found.keys()):
        problems.append(f"{module} no longer imports finance.ai or finance.chat; delete its AI_CHAT_IMPORTERS entry")
    for module, targets in sorted(found.items()):
        shims = sorted(target for target in targets if _is_ai_chat_shim(target))
        if shims:
            problems.append(f"{module} imports the old shim path {', '.join(shims)}; import from finance.ai or finance.chat")
    return problems


def test_only_allow_listed_modules_import_ai_and_chat():
    _, _, graph = finance_scan()
    problems = check_ai_chat_importers(graph, AI_CHAT_IMPORTERS)
    assert not problems, (
        "The AI and chat import contract changed. A new caller should go through an existing "
        "allow-listed module, or be added to AI_CHAT_IMPORTERS with its reason and listed under the "
        "PR's ### Gate changes:\n  " + "\n  ".join(problems)
    )


def test_every_ai_chat_importer_has_a_reason():
    for module, (targets, reason) in AI_CHAT_IMPORTERS.items():
        assert targets and all(_in_ai_chat(target) for target in targets), module
        assert reason.strip(), f"{module} needs a one-line reason"


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


def test_ai_chat_contract_fails_on_a_new_importer_a_new_target_a_stale_entry_and_a_shim():
    sources = {
        ROOT_MODULE: "",
        "ai": "",
        "ai.services": "from ..models import Person\n",
        "ai.types": "",
        "ai_services": "from finance.ai import services as _moved\n",
        "chat": "",
        "chat.services": "from ..ai.services import run\n",
        "models": "from .ai.types import LABEL\n",
        "lifecycle_services": "def purge():\n    from .chat.services import delete\n",
        "budget_services": "from .ai_services import member_has_ai\n",
        "policy_services": "",
    }
    _, _, graph = scan(sources, packages={ROOT_MODULE, "ai", "chat"})
    allowed = {
        "models": ({"ai.types"}, "labels"),
        "lifecycle_services": ({"chat.services"}, "purge"),
    }
    assert check_ai_chat_importers(graph, allowed) == [
        "budget_services imports ai_services and is not in AI_CHAT_IMPORTERS",
        "budget_services imports the old shim path ai_services; import from finance.ai or finance.chat",
    ]
    allowed["policy_services"] = ({"ai.services"}, "gone")
    allowed["models"] = ({"ai.services"}, "wrong target")
    graph.pop("budget_services")
    assert check_ai_chat_importers(graph, allowed) == [
        "models imports ai.types; AI_CHAT_IMPORTERS records ai.services",
        "policy_services no longer imports finance.ai or finance.chat; delete its AI_CHAT_IMPORTERS entry",
    ]
