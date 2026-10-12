"""Parse TRUSTED_PROXY_IPS, the addresses whose X-Forwarded-For the app honors.

Kept outside the finance app so settings can import it before Django is set up.
"""

import ipaddress
import socket
import struct

from django.core.exceptions import ImproperlyConfigured

DEFAULT_GATEWAY = "default-gateway"
ROUTE_TABLE_PATH = "/proc/net/route"
_RTF_GATEWAY = 0x2


def default_gateway_address(route_path=ROUTE_TABLE_PATH):
    """Return the IPv4 default gateway from a Linux route table, or None.

    Inside a compose container that gateway is the host side of the compose
    network, which is where a connection to the loopback-published port (and so
    Tailscale Serve) arrives from. Docker picks the subnet when it creates the
    network, so the address is read here rather than pinned in compose.yml.
    """
    try:
        with open(route_path, encoding="ascii") as route_table:
            lines = route_table.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = int(fields[2], 16)
        except ValueError:
            continue
        if flags & _RTF_GATEWAY and gateway:
            return socket.inet_ntoa(struct.pack("<L", gateway))
    return None


def parse_trusted_proxies(raw, route_path=ROUTE_TABLE_PATH):
    """Turn a comma-separated list of addresses and CIDRs into networks.

    The literal `default-gateway` stands for the container's default gateway.
    Anything unparseable stops start-up: a silently dropped entry would either
    trust nothing (every member shares the proxy's throttle bucket) or, worse,
    hide a typo in a security setting.
    """
    networks = []
    for entry in (part.strip() for part in (raw or "").split(",")):
        if not entry:
            continue
        if entry == DEFAULT_GATEWAY:
            gateway = default_gateway_address(route_path)
            if gateway is None:
                raise ImproperlyConfigured("TRUSTED_PROXY_IPS names default-gateway, but no default route was found.")
            entry = gateway
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise ImproperlyConfigured(f"TRUSTED_PROXY_IPS has an invalid entry: {entry!r}.") from None
    return tuple(networks)
