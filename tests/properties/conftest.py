"""Hypothesis profiles for the ledger property tests (#533).

`ci` is the default: derandomized and bounded, so every run explores the same
examples and the suite stays within its time budget. `deep` searches far more
inputs with fresh randomness each run; use it locally after touching an
importer, sync or report path:

    HYPOTHESIS_PROFILE=deep python -m pytest tests/properties -m property
"""

import os

from hypothesis import HealthCheck, Phase, settings

settings.register_profile(
    "ci",
    derandomize=True,
    max_examples=50,
    deadline=None,
    database=None,
    print_blob=True,
    # Shrinking still runs, so a failure reports a minimal example. The explain
    # phase is skipped: on these database-backed tests it can add minutes.
    phases=(Phase.explicit, Phase.generate, Phase.target, Phase.shrink),
    suppress_health_check=(HealthCheck.too_slow, HealthCheck.data_too_large),
)
settings.register_profile(
    "deep",
    max_examples=400,
    deadline=None,
    print_blob=True,
    suppress_health_check=(HealthCheck.too_slow, HealthCheck.data_too_large),
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
