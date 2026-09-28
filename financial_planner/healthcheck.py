"""Container health probe, shared by the Dockerfile and compose.yml.

The probe reaches the app on loopback, but Django rejects any Host header that
is not in DJANGO_ALLOWED_HOSTS. A deployment that allows only its MagicDNS
name would therefore answer a "Host: 127.0.0.1" probe with HTTP 400 and be
reported unhealthy while working, so send an allowed host instead. The
X-Forwarded-Proto header stands in for Tailscale Serve so the HTTPS redirect
does not answer first.
"""
import os
import sys
import urllib.request


def probe_host(allowed_hosts):
    """The first concrete entry of DJANGO_ALLOWED_HOSTS, else "localhost"."""
    for entry in allowed_hosts.split(","):
        host = entry.strip().lstrip(".")
        if host and host != "*":
            return host
    return "localhost"


def main():
    request = urllib.request.Request(
        "http://127.0.0.1:8000/health/",
        headers={
            "Host": probe_host(os.environ.get("DJANGO_ALLOWED_HOSTS", "")),
            "X-Forwarded-Proto": "https",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=3)
    except Exception:  # noqa: BLE001 - any failure means unhealthy
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
