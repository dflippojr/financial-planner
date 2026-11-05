"""Assistant replies render a small markdown subset, escaping first (#634). Synthetic text only."""

import json
import re
import time
from html.parser import HTMLParser

import pytest
from django.template import Context, Template
from django.test import Client
from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st

from finance.ai_services import connect_harness
from finance.chat_markdown import MAX_INLINE_DEPTH, render, safe_href
from finance.chat_runner import process_pending_turns
from finance.chat_services import send_message
from finance.models import AiConversationMessage
from tests.test_chat import TOKEN, harness, make_member  # noqa: F401 - harness is a fixture
from tests.test_security_headers import assert_page_is_csp_clean

# Every tag and attribute the renderer may write. Anything else in its output is a leak.
ALLOWED = {
    "div": {"class", "tabindex", "role", "aria-label"},
    "p": set(), "br": set(), "strong": set(), "em": set(), "code": set(), "pre": set(),
    "h3": set(), "h4": set(), "h5": set(), "h6": set(), "hr": set(), "blockquote": set(),
    "ul": set(), "ol": {"start"}, "li": set(), "a": {"href"},
    "table": set(), "thead": set(), "tbody": set(), "tr": set(), "th": {"scope", "class"}, "td": {"class"},
}
ALLOWED_CLASSES = {"chat-md", "chat-md-table", "chat-md-center", "chat-md-right"}


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def parse(html):
    parser = Markup()
    parser.feed(str(html))
    parser.close()
    return parser


def assert_only_subset(html):
    """The output holds only allowlisted tags and attributes, and every link is a same-origin path."""
    for tag, attrs in parse(html).tags:
        assert tag in ALLOWED, f"unexpected <{tag}> in {html}"
        assert set(attrs) <= ALLOWED[tag], f"unexpected attributes {attrs} on <{tag}> in {html}"
        if "class" in attrs:
            assert set(attrs["class"].split()) <= ALLOWED_CLASSES
        if tag == "a":
            assert safe_href(attrs["href"]), attrs["href"]
        if tag == "ol" and "start" in attrs:
            assert attrs["start"].isdigit()


def body(text):
    """The rendered reply without its wrapper div."""
    html = str(render(text))
    assert html.startswith('<div class="chat-md">') and html.endswith("</div>")
    return html[len('<div class="chat-md">'):-len("</div>")]


# Each element in the subset.

def test_paragraphs_and_line_breaks():
    assert body("First line.\nSecond line.\n\nNew paragraph.") == "<p>First line.<br>Second line.</p><p>New paragraph.</p>"


def test_bold_italic_and_both():
    assert body("**bold** __also__ *italic* _also_ ***both***") == (
        "<p><strong>bold</strong> <strong>also</strong> <em>italic</em> <em>also</em> <em><strong>both</strong></em></p>"
    )


@pytest.mark.parametrize(("source", "html"), [
    ("**Budget: *on track***", "<p><strong>Budget: <em>on track</em></strong></p>"),
    ("*italic **bold***", "<p><em>italic <strong>bold</strong></em></p>"),
    ("**run `a**b` now**", "<p><strong>run <code>a**b</code> now</strong></p>"),
    ("**a** and **b**", "<p><strong>a</strong> and <strong>b</strong></p>"),
])
def test_nested_emphasis_and_code_inside_emphasis(source, html):
    assert body(source) == html


def test_emphasis_ignores_snake_case_and_spaced_arithmetic():
    assert body("account_id_value costs 5 * 3 * 2") == "<p>account_id_value costs 5 * 3 * 2</p>"


def test_inline_code_keeps_markup_literal():
    assert body("Run `**not bold**` and ``a ` b``") == "<p>Run <code>**not bold**</code> and <code>a ` b</code></p>"


def test_backslash_escapes_show_the_character():
    assert body(r"\*not italic\*") == "<p>*not italic*</p>"


def test_fenced_code_block():
    assert body("```python\nx = 1 < 2\n**y**\n```\nafter") == "<pre><code>x = 1 &lt; 2\n**y**</code></pre><p>after</p>"


def test_unclosed_fence_runs_to_the_end():
    assert body("~~~\ncode") == "<pre><code>code</code></pre>"


@pytest.mark.parametrize(("source", "html"), [
    ("# Title", "<h3>Title</h3>"),
    ("## Title", "<h4>Title</h4>"),
    ("### Title", "<h5>Title</h5>"),
    ("#### Title", "<h6>Title</h6>"),
    ("###### Title", "<h6>Title</h6>"),
    ("## Closed ##", "<h4>Closed</h4>"),
    ("#hashtag", "<p>#hashtag</p>"),
])
def test_headings_never_outrank_the_page(source, html):
    assert body(source) == html


def test_unordered_list_with_one_nested_level():
    assert body("- a\n- b\n  - b1\n  - b2\n- c") == "<ul><li>a</li><li>b<ul><li>b1</li><li>b2</li></ul></li><li>c</li></ul>"


def test_each_nested_list_keeps_its_own_type():
    assert body("- Categories\n  - Rent\n- Steps\n  1. Save") == (
        "<ul><li>Categories<ul><li>Rent</li></ul></li><li>Steps<ol><li>Save</li></ol></li></ul>"
    )


TABLE_IN_ITEM = (
    '<div class="chat-md-table" tabindex="0" role="region" aria-label="Table"><table><thead><tr>'
    '<th scope="col">A</th><th scope="col">B</th><th scope="col">C</th></tr></thead>'
    "<tbody><tr><td>1</td><td>2</td><td>3</td></tr></tbody></table></div>"
)


def test_a_table_indented_under_a_list_item_renders_in_the_item():
    source = "1. Spending:\n\n   | A | B | C |\n   |---|---|---|\n   | 1 | 2 | 3 |\n2. Next"
    assert body(source) == f"<ol><li>Spending:{TABLE_IN_ITEM}</li><li>Next</li></ol>"


def test_a_fence_indented_under_a_list_item_is_a_code_block():
    assert body("- Run this:\n  ```\n  **literal**\n  ```\n- done") == (
        "<ul><li>Run this:<pre><code>**literal**</code></pre></li><li>done</li></ul>"
    )


def test_list_items_hold_further_paragraphs():
    assert body("1. one\n\n   second para\n2. two") == "<ol><li>one<p>second para</p></li><li>two</li></ol>"


def test_a_third_list_level_is_text_in_the_second():
    assert body("- a\n  - b\n    - c\n- d") == "<ul><li>a<ul><li>b<br>- c</li></ul></li><li>d</li></ul>"


def test_ordered_list_keeps_its_start_and_nests():
    assert body("3. three\n4. four\n   1. sub") == '<ol start="3"><li>three</li><li>four<ol><li>sub</li></ol></li></ol>'


def test_list_items_continue_and_end_at_a_blank_line():
    assert body("- item\n  more\n\nAfter") == "<ul><li>item<br>more</li></ul><p>After</p>"


def test_pipe_table_with_alignment_and_ragged_rows():
    html = body("| Month | Spent | Note |\n|:--|--:|:-:|\n| Jan | **$10.00** |\n| Feb | $2.00 | x | extra |")
    assert html == (
        '<div class="chat-md-table" tabindex="0" role="region" aria-label="Table"><table><thead><tr>'
        '<th scope="col">Month</th><th scope="col" class="chat-md-right">Spent</th><th scope="col" class="chat-md-center">Note</th>'
        "</tr></thead><tbody>"
        '<tr><td>Jan</td><td class="chat-md-right"><strong>$10.00</strong></td><td class="chat-md-center"></td></tr>'
        '<tr><td>Feb</td><td class="chat-md-right">$2.00</td><td class="chat-md-center">x</td></tr>'
        "</tbody></table></div>"
    )


def test_pipes_without_a_delimiter_row_stay_text():
    assert body("a | b\nc | d") == "<p>a | b<br>c | d</p>"


def test_escaped_pipe_stays_inside_a_cell():
    assert "<td>a | b</td>" in body("| x |\n|---|\n| a \\| b |")


def test_block_quote_nests_and_holds_blocks():
    assert body("> **Note**\n> - one\n>> deeper") == (
        "<blockquote><p><strong>Note</strong></p><ul><li>one</li></ul><blockquote><p>deeper</p></blockquote></blockquote>"
    )


def test_quotes_deeper_than_the_limit_are_text():
    assert body(">>>>> deep") == "<blockquote><blockquote><blockquote><p>&gt;&gt; deep</p></blockquote></blockquote></blockquote>"


def test_rule():
    assert body("above\n\n---\n\nbelow") == "<p>above</p><hr><p>below</p>"


def test_empty_reply_renders_an_empty_wrapper():
    assert str(render("")) == '<div class="chat-md"></div>'
    assert str(render(None)) == '<div class="chat-md"></div>'


# Links: only same-origin paths.

@pytest.mark.parametrize("url", ["/transactions/", "/spending/?month=2026-01&scope=mine", "/a#b", "/"])
def test_same_origin_paths_become_links(url):
    html = body(f"[Open]({url})")
    assert parse(html).tags == [("p", {}), ("a", {"href": url})]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "http://evil.example", "https://evil.example",
    "//evil.example", "/\\evil.example", "/ok\\..\\x", "mailto:a@example.com", "evil.example", "transactions/",
    "/a\"onmouseover=\"x", "/a'b", "/a<b", "/a`b", "/a\x00b", "/a\x7fb", "/a b",
])
def test_other_links_stay_text(url):
    html = body(f"[Open]({url})")
    assert "<a" not in html
    assert not safe_href(url)


def test_whitespace_in_a_url_is_never_a_link():
    for source in ["[x](/\t/evil.example)", "[x](/ /evil.example)", '[x](/ok "title")', "[x](\n/ok)"]:
        assert "<a" not in body(source)


def test_bare_urls_are_not_linked():
    assert body("See https://evil.example and /transactions/") == "<p>See https://evil.example and /transactions/</p>"


def test_link_text_cannot_nest_a_link():
    html = body("[[inner](/a)](/b)")
    assert html.count("<a ") == 1


# XSS cases: all of them show as text.

XSS = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "<svg onload=alert(1)>",
    '<a href="javascript:alert(1)">x</a>',
    "[click](javascript:alert(1))",
    '[click](/ok" onclick="alert(1))',
    "[click](/ok'onclick='alert(1)')",
    '[x" onmouseover="alert(1)](/ok)',
    "`<script>alert(1)</script>`",
    "``<img src=x onerror=alert(1)>`",
    "```\n</code></pre><script>alert(1)</script>\n```",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "&#60;img src=x onerror=alert(1)&#62;",
    "| a | b |\n|---|---|\n| <script>alert(1)</script> | <img src=x onerror=alert(1)> |",
    "# <iframe src=//evil.example>",
    "> <style>body{display:none}</style>",
    "- <b onclick=alert(1)>x</b>",
    "**<script>alert(1)</script>**",
]


@pytest.mark.parametrize("source", XSS)
def test_xss_cases_render_as_inert_text(source):
    html = render(source)
    assert_only_subset(html)
    tags = {tag for tag, _attrs in parse(html).tags}
    assert not tags & {"script", "img", "svg", "iframe", "style", "b"}
    assert "<script" not in html and "<img" not in html


def test_entities_in_a_reply_show_literally():
    assert body("&lt;b&gt; &amp; &#60;") == "<p>&amp;lt;b&amp;gt; &amp;amp; &amp;#60;</p>"


def test_link_text_with_quotes_is_escaped_inside_the_link():
    assert body('[say "hi" <b>](/ok)') == '<p><a href="/ok">say &quot;hi&quot; &lt;b&gt;</a></p>'


MARKDOWN_PIECES = [
    *"ab <>/\"'&`*_#|-:[]()\\\n\t1.=",
    "javascript:", "//", "/x", "<script>", "```", "|---|", "- ", "1. ", "> ", "## ", "](", "**", "\n\n",
]


@hypothesis_settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(MARKDOWN_PIECES), max_size=60).map("".join) | st.text(max_size=200))
def test_any_text_renders_only_the_subset(source):
    assert_only_subset(render(source))


def test_pathological_emphasis_stays_fast():
    started = time.monotonic()
    for source in ["*a " * 5000, "**a " * 5000, "_a " * 5000, "`" * 5000 + "a", "[a](" * 5000, ">" * 5000, "- a\n" * 5000]:
        render(source)
    assert time.monotonic() - started < 5


def _emphasis_depth(html):
    depth = deepest = 0
    for closing, _tag in re.findall(r"<(/?)(em|strong)>", html):
        depth += -1 if closing else 1
        deepest = max(deepest, depth)
    return deepest


@pytest.mark.parametrize("delimiter", ["*", "**", "***", "_"])
def test_deeply_nested_emphasis_stops_at_the_depth_limit(delimiter):
    html = str(render(delimiter * 300 + "x" + delimiter * 300))
    # *** writes em and strong per level.
    assert 1 < _emphasis_depth(html) <= 2 * MAX_INLINE_DEPTH
    assert_only_subset(html)


def test_template_filter_matches_render():
    reply = "# Plan\n\n- **Rent**: $1,000.00"
    out = Template("{% load ui %}{{ reply|chat_markdown_html }}").render(Context({"reply": reply}))
    assert out == str(render(reply))


# Saved and live turns show the same markup; user and error turns stay plain text.

REPLY = (
    "## January summary\n\n"
    "Spending was **steady**. See [transactions](/transactions/).\n\n"
    "- Rent\n- Groceries\n\n"
    "| Category | Amount | Change |\n|---|---:|---:|\n| Rent | $1,000.00 | 0% |\n| Dining | $120.00 | +5% |\n\n"
    "<img src=x onerror=alert(1)>"
)


def _member_with_reply(harness, answer, username="markdown"):  # noqa: F811 - pytest fixture value
    state, url = harness
    user, person, _household = make_member(username)
    connect_harness(person, base_url=url, token=TOKEN)
    state.session_answer = answer
    conversation = send_message(person, "**Summarize** January <b>please</b>")
    assert process_pending_turns(sleep=lambda _s: None) == 1
    client = Client()
    client.force_login(user)
    turn = conversation.messages.get(role=AiConversationMessage.Role.ASSISTANT)
    return client, conversation, turn


@pytest.mark.django_db
def test_saved_and_live_paths_render_the_same_markup(harness):  # noqa: F811 - pytest fixture
    client, conversation, turn = _member_with_reply(harness, REPLY)
    expected = str(render(REPLY))

    live = json.loads(client.get(reverse("chat-turn", args=[turn.pk])).content)
    page = client.get(reverse("chat") + f"?c={conversation.pk}")
    popover = client.get(reverse("chat-popover") + f"?c={conversation.pk}", HTTP_ACCEPT="application/json").json()

    assert live["content"] == REPLY
    assert live["content_html"] == expected
    assert expected in page.content.decode()
    assert expected in popover["html"]
    assert_page_is_csp_clean(page)
    assert "<h4>January summary</h4>" in expected
    assert "<strong>steady</strong>" in expected
    assert '<a href="/transactions/">transactions</a>' in expected
    assert "<th scope=\"col\">Category</th>" in expected
    assert "&lt;img src=x onerror=alert(1)&gt;" in expected


@pytest.mark.django_db
def test_user_turns_stay_plain_text(harness):  # noqa: F811 - pytest fixture
    client, conversation, _turn = _member_with_reply(harness, "ok", username="plain-user")
    page = client.get(reverse("chat") + f"?c={conversation.pk}").content.decode()
    assert '<p class="whitespace-pre-wrap">**Summarize** January &lt;b&gt;please&lt;/b&gt;</p>' in page


@pytest.mark.django_db
def test_error_turns_stay_plain_text_on_both_paths(harness):  # noqa: F811 - pytest fixture
    client, conversation, turn = _member_with_reply(harness, "ok", username="plain-error")
    turn.role = AiConversationMessage.Role.ERROR
    turn.content = "**Failed** <b>here</b>"
    turn.save(update_fields=("role", "content"))

    live = json.loads(client.get(reverse("chat-turn", args=[turn.pk])).content)
    page = client.get(reverse("chat") + f"?c={conversation.pk}").content.decode()

    assert live["content_html"] is None
    assert live["content"] == "**Failed** <b>here</b>"
    assert '<p class="whitespace-pre-wrap">**Failed** &lt;b&gt;here&lt;/b&gt;</p>' in page
    assert "<strong>Failed</strong>" not in page
