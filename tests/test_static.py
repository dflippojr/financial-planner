import hashlib
import struct
import zlib
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import Client, RequestFactory
from django.urls import reverse

from finance.middleware import LoginRequiredExceptStaticMiddleware, _static_prefix

from finance.models import Household, Membership, Person


PASSWORD = "Synthetic-passphrase-42!"


def _member():
    user = get_user_model().objects.create_user(username="nav-member", password=PASSWORD)
    person = Person.objects.create(user=user, display_name="Nav Member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    return user


@pytest.mark.django_db
def test_compiled_css_and_theme_script_are_public(tmp_path, settings):
    static_dir = tmp_path / "static"
    (static_dir / "dist").mkdir(parents=True)
    (static_dir / "js").mkdir(parents=True)
    (static_dir / "dist" / "app.css").write_text("/* synthetic-app-css */")
    (static_dir / "js" / "theme.js").write_text("/* synthetic-theme-js */")
    collected = tmp_path / "staticfiles"
    settings.STATICFILES_DIRS = [static_dir]
    settings.STATIC_ROOT = collected
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
    settings.WHITENOISE_AUTOREFRESH = True
    call_command("collectstatic", "--noinput", verbosity=0)

    client = Client()
    css = client.get("/static/dist/app.css")
    script = client.get("/static/js/theme.js")
    missing = client.get("/static/not-a-real-file.css")
    css_body = b"".join(css.streaming_content)
    script_body = b"".join(script.streaming_content)

    assert css.status_code == 200
    assert "text/css" in css["Content-Type"]
    assert b"synthetic-app-css" in css_body
    assert script.status_code == 200
    assert b"synthetic-theme-js" in script_body
    assert missing.status_code == 404


@pytest.mark.django_db
def test_default_deny_still_protects_non_static_paths():
    _member()
    client = Client()
    unknown = client.get("/not-a-public-page/")
    transactions = client.get(reverse("transaction-list"))
    invite = client.get(reverse("invite"))
    health = client.get(reverse("health"))
    login = client.get(reverse("login"))

    assert unknown.status_code == 404
    assert transactions.status_code == 302
    assert transactions.url.startswith(reverse("login"))
    assert invite.status_code == 302
    assert invite.url.startswith(reverse("login"))
    assert health.status_code == 200
    assert login.status_code == 200


@pytest.mark.django_db
def test_pages_do_not_request_third_party_hosts(client):
    _member()
    login = client.get(reverse("login"))
    content = login.content.decode()

    assert login.status_code == 200
    assert "cdn." not in content.lower()
    assert "googleapis.com" not in content
    assert "fonts.gstatic.com" not in content
    assert "/static/dist/app.css" in content
    assert "/static/js/theme.js" in content


def test_theme_script_notifies_charts_and_charts_stay_self_hosted():
    root = Path(__file__).resolve().parent.parent
    theme = (root / "static" / "js" / "theme.js").read_text(encoding="utf-8")
    charts = (root / "static" / "js" / "charts.js").read_text(encoding="utf-8")

    assert "financial-planner:themechange" in theme
    assert "financial-planner:themechange" in charts
    assert "cssVarColor" in charts
    assert "cdn." not in charts.lower()
    assert "https://" not in charts


@pytest.mark.django_db
def test_signed_in_pages_use_shared_nav_and_signed_out_pages_use_a_card():
    user = _member()
    client = Client()
    client.force_login(user)
    home = client.get(reverse("home")).content.decode()
    login = Client().get(reverse("login")).content.decode()

    for label in (
        "Cash flow",
        "Net worth",
        "Spending",
        "Transactions",
        "Transfers",
        "Recurring",
        "Accounts",
        "Import",
        "Planning",
        "Planned items",
    ):
        assert label in home
    assert reverse("category-list") not in home
    assert reverse("invite") not in home
    assert reverse("simplefin-connections") not in home
    assert 'aria-current="page"' in home
    assert 'aria-label="Settings"' in home
    assert 'data-tip="Settings"' in home
    assert 'id="theme-toggle"' in home
    assert 'aria-label="Switch to dark theme"' in home
    assert 'aria-pressed="false"' in home
    assert 'aria-label="Sign out"' in home
    assert reverse("logout") in home
    assert "csrfmiddlewaretoken" in home
    assert "/static/vendor/chart.umd.min.js" in home
    assert "/static/js/charts.js" in home
    assert "cdn." not in home.lower()
    assert "drawer" in home
    assert "card-body" in login
    assert "drawer" not in login
    assert "<p><a href=" not in home


def test_static_url_prefix_is_normalized(settings):
    settings.STATIC_URL = "static"
    assert _static_prefix() == "/static/"
    settings.STATIC_URL = "/static"
    assert _static_prefix() == "/static/"
    settings.STATIC_URL = None
    assert _static_prefix() == "/static/"


def test_login_required_middleware_skips_static_paths(settings):
    settings.STATIC_URL = "/static/"
    middleware = LoginRequiredExceptStaticMiddleware(lambda request: None)
    request = RequestFactory().get("/static/dist/app.css")
    request.user = AnonymousUser()
    assert middleware.process_view(request, lambda: None, (), {}) is None


def test_vendored_chartjs_matches_recorded_checksum():
    vendor = Path(__file__).resolve().parent.parent / "static" / "vendor"
    recorded = {}
    for line in (vendor / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, name = line.split()
        recorded[name] = digest
    blob = (vendor / "chart.umd.min.js").read_bytes()

    assert recorded["chart.umd.min.js"] == hashlib.sha256(blob).hexdigest()
    assert b"Chart.js v4.5.1" in blob
    assert b"window.Chart" in blob
    assert b"sourceMappingURL" not in blob
    assert (vendor / "LICENSE.md").read_text(encoding="utf-8").startswith("The MIT License")


@pytest.mark.django_db
def test_collectstatic_accepts_vendored_chartjs_without_a_source_map(tmp_path, settings):
    root = Path(__file__).resolve().parent.parent
    static_dir = tmp_path / "static"
    vendor = static_dir / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "chart.umd.min.js").write_bytes((root / "static" / "vendor" / "chart.umd.min.js").read_bytes())
    collected = tmp_path / "staticfiles"
    settings.STATICFILES_DIRS = [static_dir]
    settings.STATIC_ROOT = collected
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
    call_command("collectstatic", "--noinput", verbosity=0)

    hashed = list(collected.rglob("chart.umd.min.js*"))
    assert hashed
    assert not any(path.name.endswith(".map") for path in collected.rglob("*"))


def _png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _paeth_predictor(left, up, up_left):
    estimate = left + up - up_left
    distances = (abs(estimate - left), abs(estimate - up), abs(estimate - up_left))
    return (left, up, up_left)[distances.index(min(distances))]


def _png_corner_pixels(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert bit_depth == 8
    assert color_type in (2, 6)
    channels = 3 if color_type == 2 else 4
    offset = 8
    compressed = b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"IDAT":
            compressed += chunk
        offset += 12 + length
    raw = zlib.decompress(compressed)
    stride = width * channels
    previous = bytearray(stride)
    rows = []
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        filtered = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        reconstructed = bytearray(stride)
        for index, value in enumerate(filtered):
            left = reconstructed[index - channels] if index >= channels else 0
            up = previous[index]
            up_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                reconstructed[index] = value
            elif filter_type == 1:
                reconstructed[index] = (value + left) % 256
            elif filter_type == 2:
                reconstructed[index] = (value + up) % 256
            elif filter_type == 3:
                reconstructed[index] = (value + (left + up) // 2) % 256
            elif filter_type == 4:
                reconstructed[index] = (value + _paeth_predictor(left, up, up_left)) % 256
            else:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
        rows.append(bytes(reconstructed))
        previous = reconstructed
    def pixel(x, y):
        start = x * channels
        return tuple(rows[y][start : start + channels])

    return pixel(0, 0), pixel(width - 1, 0), pixel(0, height - 1), pixel(width - 1, height - 1)


def _repo_root():
    return Path(__file__).resolve().parent.parent


def test_committed_icons_and_manifest_are_install_sized():
    root = _repo_root()
    assert _png_size(root / "static" / "icons" / "apple-touch-icon.png") == (180, 180)
    assert _png_size(root / "static" / "icons" / "icon-192.png") == (192, 192)
    assert _png_size(root / "static" / "icons" / "icon-512.png") == (512, 512)
    svg = (root / "static" / "icons" / "icon.svg").read_text(encoding="utf-8")
    manifest = (root / "static" / "manifest.webmanifest").read_text(encoding="utf-8")

    assert 'fill="#422ad5"' in svg
    assert '<rect width="512" height="512" fill="#422ad5"/>' in svg
    assert 'rx="96"' not in svg
    assert '"name": "Financial Planner"' in manifest
    assert '"short_name": "Finances"' in manifest
    assert '"display": "standalone"' in manifest
    assert "/static/icons/icon-192.png" in manifest
    assert "/static/icons/icon-512.png" in manifest
    assert "maskable" in manifest
    assert "serviceWorker" not in manifest


def test_home_screen_icons_are_full_bleed_background():
    background = (0x42, 0x2A, 0xD5)
    root = _repo_root() / "static" / "icons"
    for name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        corners = _png_corner_pixels(root / name)
        assert all(pixel[:3] == background for pixel in corners)
        assert all(len(pixel) < 4 or pixel[3] == 255 for pixel in corners)


@pytest.mark.django_db
def test_pages_link_install_metadata_and_do_not_register_a_service_worker():
    user = _member()
    login = Client().get(reverse("login")).content.decode()
    signed_in = Client()
    signed_in.force_login(user)
    home = signed_in.get(reverse("home")).content.decode()

    for content in (login, home):
        assert 'rel="manifest"' in content
        assert "/static/manifest.webmanifest" in content
        assert "/static/icons/apple-touch-icon.png" in content
        assert 'name="theme-color"' in content
        assert "(prefers-color-scheme: light)" in content
        assert "(prefers-color-scheme: dark)" in content
        assert 'content="#ffffff"' in content
        assert 'content="#1d232a"' in content
        assert "viewport-fit=cover" in content
        assert "apple-mobile-web-app-capable" in content
        assert "serviceWorker.register" not in content
        assert "navigator.serviceWorker" not in content
        assert "cdn." not in content.lower()


def test_templates_and_scripts_do_not_register_a_service_worker():
    root = _repo_root()
    scanned = 0
    for directory in (root / "templates", root / "static" / "js"):
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() in {".png"}:
                continue
            text = path.read_text(encoding="utf-8")
            scanned += 1
            assert "serviceWorker.register" not in text
            assert "navigator.serviceWorker" not in text
    assert scanned > 0


def _css_layer_block(css, layer_name):
    marker = f"@layer {layer_name} {{"
    start = css.index(marker)
    depth = 0
    for index, char in enumerate(css[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"unclosed @layer {layer_name}")


def test_compact_layout_css_uses_safe_area_insets():
    css = (_repo_root() / "static" / "src" / "app.css").read_text(encoding="utf-8")

    for inset in (
        "env(safe-area-inset-top)",
        "env(safe-area-inset-right)",
        "env(safe-area-inset-bottom)",
        "env(safe-area-inset-left)",
    ):
        assert inset in css


def test_navbar_safe_area_padding_is_outside_base_layer():
    css = (_repo_root() / "static" / "src" / "app.css").read_text(encoding="utf-8")
    base = _css_layer_block(css, "base")
    navbar_top = "padding-top: max(0.5rem, env(safe-area-inset-top))"
    outside_base = css.replace(base, "", 1)

    assert navbar_top not in base
    assert navbar_top in outside_base
    assert "body:not(:has(.navbar))" in css
    assert "padding-top: env(safe-area-inset-top)" in base


@pytest.mark.django_db
def test_manifest_and_icons_are_public_static_files(tmp_path, settings):
    root = _repo_root()
    static_dir = tmp_path / "static"
    icons = static_dir / "icons"
    icons.mkdir(parents=True)
    (static_dir / "manifest.webmanifest").write_bytes(
        (root / "static" / "manifest.webmanifest").read_bytes()
    )
    for name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        (icons / name).write_bytes((root / "static" / "icons" / name).read_bytes())
    collected = tmp_path / "staticfiles"
    settings.STATICFILES_DIRS = [static_dir]
    settings.STATIC_ROOT = collected
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
    settings.WHITENOISE_AUTOREFRESH = True
    call_command("collectstatic", "--noinput", verbosity=0)

    client = Client()
    manifest = client.get("/static/manifest.webmanifest")
    icon = client.get("/static/icons/icon-192.png")
    apple = client.get("/static/icons/apple-touch-icon.png")
    manifest_body = b"".join(manifest.streaming_content)
    icon_body = b"".join(icon.streaming_content)

    assert manifest.status_code == 200
    assert "application/manifest+json" in manifest["Content-Type"]
    assert b'"display": "standalone"' in manifest_body
    assert icon.status_code == 200
    assert apple.status_code == 200
    assert icon_body[:8] == b"\x89PNG\r\n\x1a\n"


