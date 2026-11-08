"""The privacy sweeps' canary amounts are too large to collide with an object id (#709)."""
from tests.privacy_world import FORBIDDEN
from tests.test_route_access_sweep import OUTSIDER_FORBIDDEN

# A full PostgreSQL run takes primary-key sequences to about 15,000; a canary's bare
# minor string must stay far above anything an id can reach.
ID_CEILING = 100_000


def test_every_canary_minor_value_is_too_large_to_be_an_id():
    minors = sorted({int(needle) for needle in (*FORBIDDEN, *OUTSIDER_FORBIDDEN) if needle.isdigit()})
    small = [minor for minor in minors if minor <= ID_CEILING]
    assert not small, f"canary minor values an id can reach: {small}"
