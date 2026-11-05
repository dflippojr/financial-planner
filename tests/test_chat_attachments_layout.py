"""Attaching a file in Chromium (#644): the Chat page on phone and desktop, and the desktop popover.

The warning names the provider before anything is uploaded, the controls keep 44 px targets, the
page never scrolls sideways, and no Content Security Policy violation is reported.
"""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from finance.models import AiChatAttachment
from tests.browser_support import SCREEN_DIR, browser, page_session  # noqa: F401
from tests.chat_attachment_fixtures import BUDGET_CSV, make_pdf
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

PROVIDER = "Claude through the agent harness"
NO_SIDEWAYS_SCROLL = "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"


def _height(locator):
    # Layout height: the popover opens with a scale animation that would shrink a bounding box.
    return locator.evaluate("(el) => el.offsetHeight")


def _csp_watch(page):
    violations = []
    page.on("console", lambda message: violations.append(message.text) if "Content Security Policy" in message.text else None)
    return violations


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_attach_a_file_on_the_chat_page(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    violations = _csp_watch(page)
    try:
        _member(url)
        page.goto(page.url.split("?")[0].rstrip("/") + "/chat/")
        page.wait_for_load_state("networkidle")
        note = page.locator("#chat-attachment-note")
        expect(note).to_be_visible()
        expect(note).to_contain_text(f"An attached file's contents go to {PROVIDER}.")
        picker = page.locator("#chat-send-form input[name=attachment]")
        expect(picker).to_be_visible()
        assert _height(picker) >= 44
        assert picker.get_attribute("aria-describedby") == "chat-attachment-note"
        assert page.evaluate(NO_SIDEWAYS_SCROLL)

        picker.set_input_files(files=[{"name": "budget.csv", "mimeType": "text/csv", "buffer": BUDGET_CSV.encode()}])
        page.locator("#chat-send-form textarea[name=prompt]").fill("Compare my budget with spending.")
        page.locator("#chat-send-form").get_by_role("button", name="Send").click()
        page.wait_for_url("**/chat/?c=*")
        chip = page.locator("main [data-chat-attachment]")
        expect(chip).to_have_count(1)
        expect(chip).to_contain_text("budget.csv")
        expect(chip).to_contain_text("CSV · 4 rows")
        chip.scroll_into_view_if_needed()
        expect(chip).to_be_visible()
        remove = chip.get_by_role("button", name="Remove budget.csv")
        assert _height(remove) >= 44
        assert page.evaluate(NO_SIDEWAYS_SCROLL)
        assert AiChatAttachment.objects.get().original_name == "budget.csv"
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"chat-attachment-{scheme}.png"))

        page.on("dialog", lambda dialog: dialog.accept())
        remove.click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("main [data-chat-attachment]")).to_have_count(0)
        assert not AiChatAttachment.objects.exists()
        assert violations == []
    finally:
        context.close()


def test_a_refused_file_shows_why_on_the_chat_page(page_session, harness):  # noqa: F811
    _state, url = harness
    context, page = page_session((390, 844), "light", "/")
    try:
        _member(url)
        page.goto(page.url.split("?")[0].rstrip("/") + "/chat/")
        page.locator("#chat-send-form input[name=attachment]").set_input_files(
            files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"plain synthetic notes"}]
        )
        page.locator("#chat-send-form textarea[name=prompt]").fill("Read this.")
        page.locator("#chat-send-form").get_by_role("button", name="Send").click()
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("status").filter(has_text="Attach a CSV, XLSX or PDF file")).to_be_visible()
        assert not AiChatAttachment.objects.exists()
    finally:
        context.close()


def test_attach_a_file_in_the_desktop_popover(page_session, harness):  # noqa: F811
    _state, url = harness
    context, page = page_session((1280, 800), "light", "/")
    violations = _csp_watch(page)
    try:
        _member(url)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.evaluate("() => window.AiPopover.open({origin: document.querySelector('main h1')})")
        popover = page.locator("#ai-popover")
        expect(popover).to_be_visible()
        note = popover.locator("#ai-popover-attachment-note")
        # The thread load fills in the member's provider before anything can be sent.
        expect(note).to_contain_text(f"An attached file's contents go to {PROVIDER}.")
        picker = popover.locator("input[name=attachment]")
        expect(picker).to_be_enabled()
        assert _height(picker) >= 44
        picker.set_input_files(files=[{"name": "statement.pdf", "mimeType": "application/pdf",
                                       "buffer": make_pdf(["Synthetic statement"])}])
        popover.locator("textarea").fill("What does this say?")
        popover.locator("#ai-popover-form").get_by_role("button", name="Send").click()
        chip = popover.locator("[data-chat-attachment]")
        expect(chip).to_have_count(1)
        expect(chip).to_contain_text("statement.pdf")
        expect(chip).to_contain_text("PDF · 1 page")
        assert picker.evaluate("(el) => el.value") == ""
        assert popover.locator("[data-pop-scroll]").evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
        assert page.evaluate(NO_SIDEWAYS_SCROLL)
        assert AiChatAttachment.objects.get().original_name == "statement.pdf"
        assert violations == []
    finally:
        context.close()


def test_a_file_chosen_while_the_popover_sends_stays_selected(page_session, harness):  # noqa: F811
    _state, url = harness
    context, page = page_session((1280, 800), "light", "/")
    try:
        _member(url)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.evaluate("() => window.AiPopover.open({origin: document.querySelector('main h1')})")
        popover = page.locator("#ai-popover")
        picker = popover.locator("input[name=attachment]")
        expect(picker).to_be_enabled()
        picker.set_input_files(files=[{"name": "first.csv", "mimeType": "text/csv", "buffer": b"a\n1\n"}])
        popover.locator("textarea").fill("First question.")
        # Hold the send open while the member picks the next file.
        gate = {"route": None}
        page.route("**/chat/send/", lambda route: gate.update(route=route))
        popover.locator("#ai-popover-form").get_by_role("button", name="Send").click()
        for _ in range(100):
            if gate["route"] is not None:
                break
            page.wait_for_timeout(50)
        assert gate["route"] is not None
        picker.set_input_files(files=[{"name": "second.csv", "mimeType": "text/csv", "buffer": b"b\n2\n"}])
        gate["route"].continue_()
        expect(popover.locator("[data-chat-attachment]")).to_contain_text("first.csv")
        assert picker.evaluate("(el) => el.files.length && el.files[0].name") == "second.csv"
    finally:
        context.close()


def test_the_popover_picker_stays_disabled_when_the_provider_is_unknown(page_session, harness):  # noqa: F811
    _state, url = harness
    context, page = page_session((1280, 800), "light", "/")
    try:
        _member(url)
        page.reload()
        page.wait_for_load_state("networkidle")
        # The thread request fails, so the warning can't name the provider: no file may be attached.
        page.route("**/chat/popover/**", lambda route: route.fulfill(status=500, body="synthetic failure"))
        page.evaluate("() => window.AiPopover.open({origin: document.querySelector('main h1')})")
        popover = page.locator("#ai-popover")
        expect(popover.locator("[data-pop-error]")).to_contain_text("Chat could not load")
        expect(popover.locator("input[name=attachment]")).to_be_disabled()
        expect(popover.locator("#ai-popover-attachment-note")).to_contain_text("your AI provider")
    finally:
        context.close()


def test_the_popover_stays_closed_on_a_phone_where_the_chat_page_attaches(page_session, harness):  # noqa: F811
    _state, url = harness
    context, page = page_session((390, 844), "light", "/")
    try:
        _member(url)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.evaluate("() => window.AiPopover && window.AiPopover.open({origin: document.querySelector('main h1')})")
        expect(page.locator("#ai-popover")).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_the_provider_switch_warning_shows_before_the_new_provider_reads(page_session, harness, viewport, scheme):  # noqa: F811
    # Owner decision 5: a reconnect (or switch) keeps files readable after a fresh, visible warning.
    from finance.ai_services import connect_harness
    from finance.chat_attachment_parsing import parse_upload
    from finance.chat_attachments import provider_key
    from finance.chat_services import chat_backend_for, send_message
    from tests.chat_attachment_fixtures import upload
    from tests.test_chat import TOKEN

    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        key = provider_key(*chat_backend_for(person))
        conversation = send_message(person, "Read my budget.", attachment=parse_upload(upload("budget.csv", BUDGET_CSV.encode())),
                                    warned_provider=key)
        connect_harness(person, base_url=url, token=TOKEN)
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.wait_for_load_state("networkidle")
        warning = page.locator("main [data-chat-provider-switch]")
        warning.scroll_into_view_if_needed()
        expect(warning).to_be_visible()
        expect(warning).to_contain_text(f"This conversation's attached files will go to {PROVIDER} with your next question.")
        assert page.evaluate(NO_SIDEWAYS_SCROLL)
        page.locator("#chat-send-form textarea[name=prompt]").fill("Again, please.")
        page.locator("#chat-send-form").get_by_role("button", name="Send").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("main [data-chat-provider-switch]")).to_have_count(0)
        assert provider_key(*chat_backend_for(person)) in type(conversation).objects.get(pk=conversation.pk).attachment_providers
    finally:
        context.close()
