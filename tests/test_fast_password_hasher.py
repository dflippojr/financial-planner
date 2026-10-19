"""The suite must not pay for production-strength password hashing (issue #555).

Django's default hasher costs about half a second per hash. The suite created
or checked thousands of hashes, which was roughly two thirds of its run time
and pushed the PostgreSQL job past its 15-minute limit on slower runners.
"""

import importlib

from django.contrib.auth.hashers import identify_hasher, make_password


def test_test_settings_use_a_cheap_password_hasher():
    assert identify_hasher(make_password("synthetic-password")).algorithm == "md5"


def test_production_settings_keep_django_default_password_hashing():
    production = importlib.import_module("financial_planner.settings")
    assert not hasattr(production, "PASSWORD_HASHERS")
