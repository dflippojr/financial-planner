"""Disposable-instance guards and a real sign-in/category-persistence smoke check."""
from contextlib import contextmanager
import json
import io
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import uuid

import pytest

from scripts.ui_instance import PASSWORD, ROOT, isolated_environment, refuse_database_environment, run_css_builder, teardown


@pytest.mark.parametrize("key", ["DATABASE_URL", "DATABASE", "POSTGRES_HOST", "POSTGRES_DB", "PGHOST", "DB_URL"])
def test_refuses_database_configuration(key):
    with pytest.raises(RuntimeError, match=key):
        refuse_database_environment({key: "synthetic-but-not-disposable"})


def test_environment_discards_operator_services_and_storage(monkeypatch, tmp_path):
    for key in ["SMTP_HOST", "GOOGLE_CLIENT_SECRET", "RECEIPTS_DIR", "PRIVACY_POLICY_PATH", "OPERATOR_AUDIT_DIR"]:
        monkeypatch.setenv(key, "operator-value")
    env = isolated_environment(tmp_path)
    assert "SMTP_HOST" not in env
    assert "GOOGLE_CLIENT_SECRET" not in env
    assert "PRIVACY_POLICY_PATH" not in env
    assert env["RECEIPTS_DIR"] == str(tmp_path / "receipts")
    assert env["OPERATOR_AUDIT_DIR"] == str(tmp_path / "operator-audit")


def test_teardown_refuses_non_disposable_directory(tmp_path):
    with pytest.raises(RuntimeError, match="printed disposable directory"):
        teardown(tmp_path)


def test_cli_refuses_database_before_startup():
    env = dict(os.environ, DATABASE_URL="postgresql://synthetic.invalid/test")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ui_instance.py")],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "Refusing database environment variables: DATABASE_URL" in result.stderr
    assert "synthetic.invalid" not in result.stderr
    assert not result.stdout


def read_startup(output, log, ready):
    """Signal readiness once, then keep draining diagnostics until the child exits."""
    signaled = False
    for line in output:
        log.write(line)
        log.flush()
        if signaled:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "url" in value:
            ready.put(value)
            signaled = True
    if not signaled:
        ready.put(None)


def test_startup_reader_drains_output_after_readiness():
    ready = queue.Queue()
    log = io.StringIO()
    code = 'print(\'{"url": "synthetic-loopback"}\', flush=True); print("synthetic-log-" * 20000, flush=True)'
    with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) as process:
        read_startup(process.stdout, log, ready)
        assert process.wait(timeout=5) == 0
    assert ready.get_nowait() == {"url": "synthetic-loopback"}
    assert ready.empty()
    assert log.getvalue().count("synthetic-log-") == 20000


@contextmanager
def ui_process(tmp_path, command=None, startup_timeout=90, env=None):
    # The launcher owns its SQLite storage, independent of the CI database job.
    env = isolated_environment(tmp_path) if env is None else dict(env)
    # pytest-django's settings must not carry over into the standalone process.
    env.pop("DJANGO_SETTINGS_MODULE", None)
    ready = queue.Queue()
    log = tmp_path / "ui-instance.log"
    registry = ROOT / "test-results" / "ui-instances"
    registry.mkdir(parents=True, exist_ok=True)
    handle = registry / f"pytest-{uuid.uuid4().hex}.json"
    command = command or [sys.executable, str(ROOT / "scripts/ui_instance.py")]
    with log.open("w") as errors:
        process = subprocess.Popen([*command, "--startup-handle", str(handle)],
                                   cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=errors, text=True)

        reader = threading.Thread(target=read_startup, args=(process.stdout, errors, ready), daemon=True)
        reader.start()
        instance = None
        try:
            instance = ready.get(timeout=startup_timeout)
            assert instance is not None, log.read_text()
            yield instance
        finally:
            directory = Path(instance["directory"]) if instance else None
            if directory is None and handle.exists():
                directory = Path(json.loads(handle.read_text())["directory"])
            try:
                if directory is not None and directory.exists() and process.poll() is None:
                    subprocess.run([sys.executable, str(ROOT / "scripts/ui_instance.py"),
                                    "--teardown", str(directory)], check=True, timeout=15)
            finally:
                # Unwind in the launcher; retain failed cleanup handles for recovery.
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"Disposable process did not exit; cleanup handle retained at {handle}") from None
                finally:
                    if process.poll() is not None:
                        reader.join(timeout=5)
                        process.stdout.close()
            assert process.returncode == 0, log.read_text()
            if directory is not None:
                assert not directory.exists(), "Teardown left synthetic storage behind"
            handle.unlink(missing_ok=True)


@pytest.fixture
def ui_instance(tmp_path):
    with ui_process(tmp_path) as instance:
        yield instance


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    # This CI smoke test must fail rather than silently skip a missing browser.
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


def test_sign_in_and_category_persist(browser, ui_instance):
    from playwright.sync_api import expect
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = context.new_page()
    base = ui_instance["url"]
    try:
        page.goto(base + "/sign-in/")
        expect(page.get_by_label("Username", exact=True)).to_have_attribute("autocomplete", "username")
        expect(page.get_by_label("Password", exact=True)).to_have_attribute("autocomplete", "current-password")
        assert page.request.get(base + "/favicon.ico").status == 200
        assert page.request.post(base + "/__ui_teardown__/").status == 403
        page.get_by_label("Username", exact=True).fill(ui_instance["username"])
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Cash flow", exact=True)).to_be_visible()
        page.get_by_role("navigation", name="Main tabs").get_by_role("link", name="Activity", exact=True).click()
        row = page.locator("tr").filter(has_text="Shell Oil 5741")
        row.get_by_role("link", name="Correct Shell Oil 5741", exact=True).click()
        edit_url = page.url
        form = page.locator('form[action$="/category/"]')
        form.get_by_label("Category", exact=True).select_option(label="Transportation")
        form.get_by_role("button", name="Save category", exact=True).click()
        page.goto(edit_url)
        expect(page.locator('form[action$="/category/"] option:checked')).to_have_text("Transportation")
        expect(page.locator("ol li").filter(has_text="Category:")).to_contain_text("Transportation")
        page.goto(base + "/transactions/")
        expect(page.locator("tr").filter(has_text="Shell Oil 5741")).to_contain_text("Transportation")
        page.goto(base + "/chat/")
        page.get_by_label("Question", exact=True).filter(visible=True).fill("How much is my synthetic spending?")
        page.get_by_role("button", name="Send", exact=True).filter(visible=True).click()
        reply = page.get_by_text("Synthetic UI reply. No real provider was contacted.", exact=True).filter(visible=True)
        expect(reply).to_be_visible(timeout=15000)
        page.reload()
        expect(reply).to_be_visible()
        page.get_by_label("Question", exact=True).filter(visible=True).fill("A synthetic follow-up question")
        page.get_by_role("button", name="Send", exact=True).filter(visible=True).click()
        expect(reply).to_have_count(2, timeout=15000)
        page.reload()
        expect(reply).to_have_count(2)
    finally:
        context.close()


@pytest.mark.parametrize("taskkill_failure", [None, "timeout", "launch-error"])
def test_readiness_timeout_cancels_stalled_builder_and_its_child(monkeypatch, tmp_path, taskkill_failure):
    monkeypatch.setenv("POSTGRES_HOST", "synthetic.invalid")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_PASSWORD", "synthetic-test-password")
    monkeypatch.setenv("FINANCIAL_PLANNER_TEST_DB", "postgres")
    port_record = tmp_path / "builder-child-port.json"
    allocation = tmp_path / "allocation.json"
    failure_record = tmp_path / "taskkill-failure.txt"
    child = tmp_path / "builder_child.py"
    child.write_text("""import json, socket, sys, time
from pathlib import Path
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    Path(sys.argv[1]).write_text(json.dumps(listener.getsockname()[1]))
    while True:
        time.sleep(0.1)
""")
    builder = tmp_path / "stalled_builder.py"
    builder.write_text("""import subprocess, sys
subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], check=True)
""")
    bootstrap = tmp_path / "startup.py"
    bootstrap.write_text(f"""import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from scripts import ui_instance

original_run = ui_instance.subprocess.run
def taskkill_probe(command, **options):
    if command[0] == 'taskkill' and {taskkill_failure!r} is not None:
        Path({str(failure_record)!r}).write_text({taskkill_failure!r})
        if {taskkill_failure!r} == 'timeout':
            raise ui_instance.subprocess.TimeoutExpired(command, options['timeout'])
        raise OSError('Synthetic taskkill launch failure')
    return original_run(command, **options)
ui_instance.subprocess.run = taskkill_probe

def stalled_css():
    Path({str(allocation)!r}).write_text(Path(sys.argv[-1]).read_text())
    ui_instance.run_css_builder([sys.executable, {str(builder)!r}, {str(child)!r}, {str(port_record)!r}])

ui_instance.build_css = stalled_css
ui_instance.main()
""")
    with pytest.raises(queue.Empty):
        with ui_process(tmp_path, command=[sys.executable, str(bootstrap)], startup_timeout=3):
            pytest.fail("Stalled startup unexpectedly reported readiness")
    directory = Path(json.loads(allocation.read_text())["directory"])
    assert not directory.exists(), "Cancelled startup left disposable storage behind"
    port = json.loads(port_record.read_text())
    with socket.socket() as client:
        client.settimeout(1)
        assert client.connect_ex(("127.0.0.1", port)) != 0, "Builder child survived cancellation"
    if os.name == "nt" and taskkill_failure:
        assert failure_record.read_text() == taskkill_failure, "Native fallback was not exercised"


def test_css_builder_reports_success_and_failure():
    run_css_builder([sys.executable, "-c", "import time; time.sleep(0.2)"])
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run_css_builder([sys.executable, "-c", "raise SystemExit(7)"])
    assert failure.value.returncode == 7
