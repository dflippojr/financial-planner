"""The old ai_* and chat_* paths are one-stage shims for finance/ai/ and finance/chat/ (#771).

Each shim makes its old path name the very same module object as the moved
module, so every name it exposed (public or private), monkeypatching through
the old path and logging stay as they were. The import contract in
test_import_contract.py checks who may import the packages; this checks the
shims themselves.
"""

import importlib
from pathlib import Path

FINANCE = Path(__file__).resolve().parent.parent / "finance"
PACKAGES = ("ai", "chat")


def _shims():
    return sorted(
        path.stem for path in FINANCE.glob("*.py") if path.stem.startswith(tuple(f"{p}_" for p in PACKAGES))
    )


def _moved():
    return sorted(
        f"{package}_{path.stem}"
        for package in PACKAGES
        for path in (FINANCE / package).glob("*.py")
        if path.stem != "__init__"
    )


def test_every_moved_module_keeps_one_shim_at_its_old_path():
    assert _shims() == _moved()
    assert len(_shims()) == 23


def test_each_old_path_is_the_moved_module():
    for name in _shims():
        package, _, new = name.partition("_")
        old = importlib.import_module(f"finance.{name}")
        assert old is importlib.import_module(f"finance.{package}.{new}"), name


def test_monkeypatching_through_an_old_path_reaches_the_moved_code(monkeypatch):
    from finance.ai import services

    monkeypatch.setattr("finance.ai_services.member_has_ai", lambda person: "patched")
    assert services.member_has_ai(None) == "patched"


def test_private_names_still_import_from_an_old_path():
    from finance.ai_services import _token  # noqa: F401 - the import itself is the check
    from finance.chat_proposals import _Stale  # noqa: F401 - the import itself is the check
