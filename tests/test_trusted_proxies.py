import ipaddress

import pytest
from django.core.exceptions import ImproperlyConfigured

from financial_planner.trusted_proxies import default_gateway_address, parse_trusted_proxies

# The shape of /proc/net/route in a compose container: addresses are
# little-endian hex, and only the 00000000 destination with the gateway flag is
# the default route.
ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"
COMPOSE_ROUTES = "\n".join(
    (
        ROUTE_HEADER,
        "eth0\t000010AC\t00000000\t0001\t0\t0\t0\t0000F0FF\t0\t0\t0",
        "eth0\t00000000\t010010AC\t0003\t0\t0\t0\t00000000\t0\t0\t0",
    )
)


def _route_table(tmp_path, text):
    path = tmp_path / "route"
    path.write_text(text + "\n", encoding="ascii")
    return path


def test_default_gateway_is_read_from_the_route_table(tmp_path):
    assert default_gateway_address(_route_table(tmp_path, COMPOSE_ROUTES)) == "172.16.0.1"


def test_default_gateway_is_none_without_a_default_route_or_table(tmp_path):
    only_local = "\n".join(COMPOSE_ROUTES.splitlines()[:2])
    assert default_gateway_address(_route_table(tmp_path, only_local)) is None
    assert default_gateway_address(tmp_path / "missing") is None


def test_trusted_proxies_accept_addresses_cidrs_and_the_default_gateway(tmp_path):
    networks = parse_trusted_proxies(" 127.0.0.1, 10.0.0.0/8 ,default-gateway,,", _route_table(tmp_path, COMPOSE_ROUTES))

    assert networks == (
        ipaddress.ip_network("127.0.0.1/32"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.1/32"),
    )


def test_empty_trusted_proxies_trust_nothing():
    assert parse_trusted_proxies("") == ()
    assert parse_trusted_proxies(None) == ()


@pytest.mark.parametrize("raw", ["not-an-address", "10.0.0.0/33", "true"])
def test_invalid_trusted_proxy_entries_stop_start_up(raw):
    with pytest.raises(ImproperlyConfigured, match="invalid entry"):
        parse_trusted_proxies(raw)


def test_default_gateway_without_a_route_stops_start_up(tmp_path):
    with pytest.raises(ImproperlyConfigured, match="no default route"):
        parse_trusted_proxies("default-gateway", tmp_path / "missing")
