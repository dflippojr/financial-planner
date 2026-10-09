"""SimpleFIN client limits: public destinations only, a byte cap and a total deadline."""

import datetime as dt
import socket
import ssl
import threading
import time
from io import BytesIO
from urllib.request import Request

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from finance import simplefin_client
from finance.simplefin_client import (
    DISALLOWED_ADDRESS,
    MAX_CLAIM_BYTES,
    TOO_LARGE,
    claim_access_url,
    fetch_accounts,
    read_capped,
    urlopen,
    vetted_addresses,
)
from finance.simplefin_errors import SimpleFinError

ACCESS_URL = "https://demo:synthetic-access-secret@bridge.example.test/simplefin"
CLAIM_URL = "https://bridge.example.test/simplefin/claim/synthetic-demo"


def answers(*addresses):
    def getaddrinfo(host, port, *args, **kwargs):
        rows = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            rows.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return rows

    return getaddrinfo


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.1.2.3",),
        ("192.168.1.20",),
        ("172.17.0.1",),
        ("100.101.102.103",),
        ("169.254.169.254",),
        ("0.0.0.0",),
        ("::1",),
        ("fe80::1%eth0",),
        ("fd7a:115c:a1e0::1",),
        ("::ffff:127.0.0.1",),
        ("224.0.0.1",),
        ("93.184.215.14", "10.0.0.5"),
    ],
)
def test_non_public_destinations_are_refused_before_connecting(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", answers(*addresses))
    connects = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connects.append(a))

    with pytest.raises(SimpleFinError) as fetched:
        fetch_accounts(ACCESS_URL)
    with pytest.raises(SimpleFinError) as claimed:
        claim_access_url(CLAIM_URL)

    assert str(fetched.value) == DISALLOWED_ADDRESS
    assert str(claimed.value) == DISALLOWED_ADDRESS
    assert "synthetic-access-secret" not in str(fetched.value)
    assert connects == []


def test_public_destination_connects_to_the_vetted_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", answers("93.184.215.14"))
    connects = []

    def refuse(address, timeout=None):
        connects.append((address, timeout))
        raise ConnectionRefusedError("synthetic")

    monkeypatch.setattr(socket, "create_connection", refuse)

    with pytest.raises(SimpleFinError, match="could not be reached"):
        fetch_accounts(ACCESS_URL)
    assert connects[0][0] == ("93.184.215.14", 443)
    assert 0 < connects[0][1] <= simplefin_client.FETCH_TIMEOUT_SECONDS


def test_each_public_answer_is_tried_in_order(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", answers("2606:4700::1111", "93.184.215.14"))
    tried = []

    def refuse(address, timeout=None):
        tried.append(address)
        raise OSError("synthetic: network unreachable")

    monkeypatch.setattr(socket, "create_connection", refuse)

    with pytest.raises(SimpleFinError, match="could not be reached"):
        fetch_accounts(ACCESS_URL)
    assert tried == [("2606:4700::1111", 443), ("93.184.215.14", 443)]


def test_connection_attempts_stop_at_the_deadline(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", answers("93.184.215.14"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("connected after the deadline"))

    with pytest.raises(Exception) as caught:
        urlopen(Request("https://bridge.example.test/accounts"), timeout=5, deadline=time.monotonic() - 1)
    assert isinstance(getattr(caught.value, "reason", None), TimeoutError)


def test_unresolvable_host_is_unreachable(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("synthetic")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(SimpleFinError, match="could not be reached"):
        fetch_accounts(ACCESS_URL)


def test_vetted_address_rejects_empty_and_unencodable_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(OSError):
        vetted_addresses("bridge.example.test", 443)

    def unencodable(*args, **kwargs):
        raise UnicodeError("label too long")

    monkeypatch.setattr(socket, "getaddrinfo", unencodable)
    with pytest.raises(OSError):
        vetted_addresses("x" * 300, 443)


class _Body:
    def __init__(self, body, chunk=None):
        self._body = BytesIO(body)
        self._chunk = chunk

    def read(self, size=-1):
        return self._body.read(self._chunk or size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_response_larger_than_the_cap_is_refused(monkeypatch):
    big = b'{"accounts": [], "pad": "' + b"x" * (simplefin_client.MAX_FETCH_BYTES + 10) + b'"}'
    monkeypatch.setattr(simplefin_client, "urlopen", lambda *a, **k: _Body(big))
    with pytest.raises(SimpleFinError) as fetched:
        fetch_accounts(ACCESS_URL)
    assert str(fetched.value) == TOO_LARGE

    monkeypatch.setattr(simplefin_client, "urlopen", lambda *a, **k: _Body(b"h" * (MAX_CLAIM_BYTES + 1)))
    with pytest.raises(SimpleFinError) as claimed:
        claim_access_url(CLAIM_URL)
    assert str(claimed.value) == TOO_LARGE


def test_a_body_trickled_past_the_deadline_is_refused(monkeypatch):
    clock = iter(range(0, 100_000, 7))
    monkeypatch.setattr(simplefin_client.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(simplefin_client, "urlopen", lambda *a, **k: _Body(b'{"accounts": []}' * 100, chunk=1))

    with pytest.raises(SimpleFinError, match="did not finish responding"):
        fetch_accounts(ACCESS_URL)


def test_body_exactly_at_the_cap_is_accepted():
    body = b"a" * 10
    assert read_capped(_Body(body, chunk=3), limit=10, deadline=time.monotonic() + 5) == body


@pytest.mark.parametrize("shape", ["deeply-nested", "huge-integer"])
def test_hostile_json_is_unreadable_not_a_crash(monkeypatch, shape):
    raw = b"[" * 100_000 + b"]" * 100_000 if shape == "deeply-nested" else b'{"n": ' + b"9" * 5000 + b"}"
    monkeypatch.setattr(simplefin_client, "urlopen", lambda *a, **k: _Body(raw))
    with pytest.raises(SimpleFinError, match="could not be read"):
        fetch_accounts(ACCESS_URL)


def _self_signed_context():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert_pem, key_pem


@pytest.fixture
def tls_server(tmp_path):
    """A one-shot local HTTPS server whose behavior each test chooses."""
    cert_pem, key_pem = _self_signed_context()
    (tmp_path / "cert.pem").write_bytes(cert_pem)
    (tmp_path / "key.pem").write_bytes(key_pem)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    client_context = ssl.create_default_context(cafile=str(tmp_path / "cert.pem"))
    listener = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve(respond):
        def run():
            listener.settimeout(5)
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with server_context.wrap_socket(conn, server_side=True) as tls:
                tls.settimeout(5)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += tls.recv(4096)
                try:
                    respond(tls, stop)
                except OSError:
                    pass

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    yield listener.getsockname()[1], client_context, serve
    stop.set()
    listener.close()


def _allow_loopback(monkeypatch):
    # The guard refuses loopback; this test server is loopback by necessity.
    monkeypatch.setattr(socket, "getaddrinfo", answers("127.0.0.1"))
    monkeypatch.setattr(simplefin_client, "_public_address", lambda value: True)


def test_guarded_connection_completes_a_real_https_exchange(monkeypatch, tls_server):
    port, context, serve = tls_server
    _allow_loopback(monkeypatch)
    # Large enough that the body arrives over many reads after urllib has
    # closed its connection object.
    body = b'{"pad": "' + b"x" * 300_000 + b'"}'
    serve(lambda tls, stop: tls.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)))

    deadline = time.monotonic() + 10
    with urlopen(Request(f"https://localhost:{port}/accounts"), timeout=5, context=context,
                 deadline=deadline) as response:
        assert read_capped(response, limit=1024 * 1024, deadline=deadline) == body


def test_guarded_connection_refuses_loopback_for_real(tls_server):
    port, context, serve = tls_server
    with pytest.raises(Exception) as caught:
        urlopen(Request(f"https://localhost:{port}/accounts"), timeout=5, context=context,
                deadline=time.monotonic() + 10)
    assert isinstance(getattr(caught.value, "reason", None), simplefin_client.DisallowedAddress)


def test_socket_reads_stop_at_the_total_deadline(monkeypatch, tls_server):
    port, context, serve = tls_server
    _allow_loopback(monkeypatch)

    def trickle(tls, stop):
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        while not stop.is_set():
            tls.sendall(b" ")
            time.sleep(0.05)

    serve(trickle)
    started = time.monotonic()
    deadline = started + 1.0
    with urlopen(Request(f"https://localhost:{port}/accounts"), timeout=5, context=context,
                 deadline=deadline) as response:
        with pytest.raises(TimeoutError):
            # One read of the full length would otherwise wait for every byte.
            response.read()
    elapsed = time.monotonic() - started
    assert 0.9 < elapsed < 4


def test_a_real_redirect_is_refused_without_a_second_connection(monkeypatch, tls_server):
    port, context, serve = tls_server
    _allow_loopback(monkeypatch)
    monkeypatch.setattr(simplefin_client, "_SSL", context)
    real_connect = socket.create_connection
    connects = []

    def counting_connect(address, *args, **kwargs):
        connects.append(address)
        return real_connect(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", counting_connect)
    serve(lambda tls, stop: tls.sendall(
        b"HTTP/1.1 302 Found\r\nLocation: https://localhost:%d/elsewhere\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n" % port))

    with pytest.raises(SimpleFinError, match="unexpected redirect"):
        fetch_accounts(f"https://demo:synthetic-access-secret@localhost:{port}/simplefin")
    assert connects == [("127.0.0.1", port)]
