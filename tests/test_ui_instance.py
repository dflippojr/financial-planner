"""Disposable-instance guards and a real sign-in/category-persistence smoke check."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from scripts.ui_instance import PASSWORD, ROOT, isolated_environment, refuse_database_environment, teardown


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


@pytest.fixture
def ui_instance(tmp_path):
    env = dict(os.environ)
    # pytest-django's settings must not carry over into the standalone process.
    env.pop("DJANGO_SETTINGS_MODULE", None)
    ready = queue.Queue()
    log = tmp_path / "ui-instance.log"
    with log.open("w") as errors:
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/ui_instance.py")],
                                   cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=errors, text=True)

        def read_ready():
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "url" in value:
                    ready.put(value)
                    return
            ready.put(None)

        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        instance = None
        try:
            instance = ready.get(timeout=90)
            assert instance is not None, log.read_text()
            yield instance
        finally:
            if instance is not None and process.poll() is None:
                subprocess.run([sys.executable, str(ROOT / "scripts/ui_instance.py"),
                                "--teardown", instance["directory"]], check=True, timeout=15)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            reader.join(timeout=1)
            if instance:
                assert process.returncode == 0, log.read_text()
                assert not Path(instance["directory"]).exists(), "Teardown left synthetic storage behind"


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
    finally:
        context.close()
