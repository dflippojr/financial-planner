"""Keep every operator journal written by tests in disposable storage."""
import os
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def disposable_operator_journal(tmp_path_factory):
    values = {"OPERATOR_AUDIT_DIR": str(tmp_path_factory.mktemp("operator-audit")), "AUDIT_PYTHON": sys.executable}
    before = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
