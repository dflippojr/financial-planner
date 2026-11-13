"""Keep every operator journal written by tests in disposable storage."""
import os
import sys
import threading
from contextlib import contextmanager

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


@pytest.fixture(autouse=True)
def disposable_csv_staging(settings, tmp_path):
    # Without this, staged uploads go to the shared $TMPDIR/financial-planner-csv-imports,
    # where every earlier run's leftovers count toward the per-user staging limit (#696).
    # The setting, not the environment variable: settings.py has already read that.
    settings.CSV_IMPORT_STAGING_DIR = str(tmp_path / "csv-staging")


# Only tables still missing the setting are altered, so a pass with nothing to fix
# is one catalog scan.
AUTOVACUUM_OFF = (
    "DO $$ DECLARE t text; BEGIN "
    "FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
    "AND NOT COALESCE('autovacuum_enabled=false' = ANY (c.reloptions), false) LOOP "
    "EXECUTE format('ALTER TABLE %I SET (autovacuum_enabled = false)', t); "
    "END LOOP; END $$"
)


def _turn_autovacuum_off(django_db_blocker):
    from django.db import connection

    if connection.vendor != "postgresql":
        return
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(AUTOVACUUM_OFF)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    # Keep PostgreSQL's planner statistics as created. Autovacuum vacuums the
    # tables while rollbacks leave them empty and records reltuples = 0, after
    # which the transfer-exclusion subquery plans as a self-join of every
    # transaction and one test ran for ten minutes (#555). Without statistics
    # the planner sizes each table from its pages, as it does for real data.
    _turn_autovacuum_off(django_db_blocker)


def _is_transactional(item):
    # The same test pytest-django uses to pick TransactionTestCase.
    from pytest_django.fixtures import validate_django_db

    marker = item.get_closest_marker("django_db")
    if marker:
        transaction, reset_sequences, *_ = validate_django_db(marker)
        if transaction or reset_sequences:
            return True
    fixtures = getattr(item, "fixturenames", ())
    return any(name in fixtures for name in ("transactional_db", "live_server", "django_db_reset_sequences"))


@pytest.fixture(autouse=True)
def keep_autovacuum_off(request):
    # Migration tests run with transaction=True and migrate backward and
    # forward, which drops and recreates tables without the setting (#596).
    yield
    if _is_transactional(request.node):
        _turn_autovacuum_off(request.getfixturevalue("django_db_blocker"))


class _OneRequestAtATime:
    """Lets one live-server request at a time use a connection shared with every request thread.

    The lock is taken in the WSGI app, after the request line has been read, so an idle keep-alive
    socket never holds it. It is released when the server finishes that request, after the
    response has been written and closed, whatever path the request took.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def acquire(self):
        self._lock.acquire()
        self._owner = threading.get_ident()

    def release(self):
        # Only the request thread that took the lock compares equal here.
        if self._owner == threading.get_ident():
            self._owner = None
            self._lock.release()

    def wrap_app(self, app):
        def one_at_a_time(environ, start_response):
            self.acquire()
            return app(environ, start_response)

        return one_at_a_time

    def wrap_handler(self, handler_class):
        gate = self

        class OneRequestAtATimeHandler(handler_class):
            def handle_one_request(self):
                try:
                    super().handle_one_request()
                finally:
                    gate.release()

        return OneRequestAtATimeHandler


@pytest.fixture(scope="session")
def live_server(live_server):
    # With in-memory SQLite, pytest-django hands the connection it finds at start-up to every request
    # thread. Two requests inside atomic() at once (the Chat page polls each pending turn, and each
    # poll opens one) corrupt its savepoint bookkeeping, and since an in-memory connection is never
    # closed, every later live-server test on that xdist worker failed (#653). Serialize them.
    live_server.request_gate = None
    if live_server.thread.connections_override:
        gate = _OneRequestAtATime()
        server = live_server.thread.httpd
        server.RequestHandlerClass = gate.wrap_handler(server.RequestHandlerClass)
        server.set_app(gate.wrap_app(server.get_app()))
        live_server.request_gate = gate
    return live_server


@pytest.fixture
def live_requests_paused(live_server):
    """Return a context manager that holds off live-server requests while the test uses the database.

    A browser test's thread does not share the live server's in-memory SQLite connection: Playwright
    runs an asyncio loop in that thread, so Django's connections there come from asgiref's
    context-variable storage and the test thread opens a second connection to the shared-cache
    database. Shared-cache connections lock whole tables and fail at once with "database table is
    locked" rather than wait, so a test-thread write while a request reads the same table fails
    (#738). Inside the block no request runs: entering waits for the one in flight to finish, and
    requests the page sends meanwhile wait until the block ends. Make no Playwright call inside it,
    since the page's requests could not be answered. On other databases it does nothing.
    """
    gate = live_server.request_gate

    @contextmanager
    def paused():
        if gate is None:
            yield
            return
        gate.acquire()
        try:
            yield
        finally:
            gate.release()

    return paused


@pytest.fixture(autouse=True)
def restore_harness_origins():
    # Fake harnesses add their random-port origin to the allow-list (#409).
    from django.conf import settings

    before = list(settings.AGENT_HARNESS_ALLOWED_ORIGINS)
    yield
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = before


def _uses_browser(item):
    return "browser" in getattr(item, "fixturenames", ())


def pytest_collection_modifyitems(config, items):
    # Every Playwright test reaches Chromium through a fixture named "browser",
    # so CI can select them with -m browser without a marker in each module.
    for item in items:
        if _uses_browser(item):
            item.add_marker(pytest.mark.browser)


class _BrowserTraces:
    """Record a Playwright trace per browser test and keep only failures' traces.

    Enabled by PLAYWRIGHT_TRACE_DIR (CI sets it). Tests open contexts through
    Browser.new_context or Browser.new_page and close them in their own
    finally blocks, so tracing starts on open and is saved on close.
    """

    def __init__(self, directory):
        self.directory = directory
        self.test_id = None
        self.open = []
        self.saved = []
        self.failed = False

    def install(self):
        from playwright.sync_api import Browser, BrowserContext

        plugin = self
        original_new_context = Browser.new_context
        original_close = BrowserContext.close

        def new_context(browser, *args, **kwargs):
            context = original_new_context(browser, *args, **kwargs)
            if plugin.test_id is not None:
                context.tracing.start(screenshots=True, snapshots=True)
                plugin.open.append(context)
            return context

        def new_page(browser, *args, **kwargs):
            # Browser.new_page makes its context below the sync API; make it here so it is traced.
            context = browser.new_context(*args, **kwargs)
            return context.new_page()

        def close(context, *args, **kwargs):
            plugin.stop(context)
            return original_close(context, *args, **kwargs)

        Browser.new_context = new_context
        Browser.new_page = new_page
        BrowserContext.close = close

    def stop(self, context):
        if context not in self.open:
            return
        self.open.remove(context)
        name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in self.test_id)[-150:]
        path = os.path.join(self.directory, f"{name}-{len(self.saved)}.zip")
        try:
            context.tracing.stop(path=path)
            self.saved.append(path)
        except Exception:  # noqa: BLE001, S110 - a lost trace must never fail the test
            pass

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.test_id = item.nodeid if _uses_browser(item) else None
        self.saved = []
        self.failed = False

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        self.failed = self.failed or report.failed
        if self.test_id is None:
            return
        if report.when == "call":
            # Save contexts the test left open (a page from new_page) while the browser is still up.
            for context in list(self.open):
                self.stop(context)
        if report.when != "teardown":
            return
        if not self.failed:
            for path in self.saved:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.test_id = None


def pytest_addoption(parser):
    parser.addoption(
        "--coverage-shard",
        metavar="K/N",
        help="run only shard K of N (1-based) of the selected tests; sonar.yml splits its coverage run this way",
    )


def _parse_shard(value):
    try:
        index, count = (int(part) for part in value.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--coverage-shard expects K/N, for example 1/3, not {value!r}") from None
    if not 1 <= index <= count:
        raise pytest.UsageError(f"--coverage-shard {value}: K must be between 1 and N")
    return index, count


class _CoverageShard:
    """Keep one of N disjoint parts of the selected tests (issue #691).

    Sorting by node id, rather than using collection order, gives every
    process the same split even if collection order varied. Taking every Nth
    test spreads each module, including the slow property tests that sit
    together, across all shards. scripts/check_coverage_shards.py proves from
    each shard's JUnit report that the shards together ran every selected
    test exactly once.
    """

    def __init__(self, index, count):
        self.index = index
        self.count = count

    # trylast: after -m has deselected tests, so the split is of the tests
    # that will run.
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        order = {nodeid: position for position, nodeid in enumerate(sorted(item.nodeid for item in items))}
        keep, drop = [], []
        for item in items:
            (keep if order[item.nodeid] % self.count == self.index - 1 else drop).append(item)
        if drop:
            config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_configure(config):
    shard = config.getoption("--coverage-shard")
    if shard:
        config.pluginmanager.register(_CoverageShard(*_parse_shard(shard)), "coverage-shard")

    directory = os.environ.get("PLAYWRIGHT_TRACE_DIR")
    if not directory:
        return
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return
    os.makedirs(directory, exist_ok=True)
    traces = _BrowserTraces(directory)
    traces.install()
    config.pluginmanager.register(traces, "browser-traces")
