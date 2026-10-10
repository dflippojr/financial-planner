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
        except Exception:  # noqa: BLE001 - a lost trace must never fail the test
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


def pytest_configure(config):
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
