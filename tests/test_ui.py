import re

import pytest
from django import forms
from django.template import Context, Template
from django.template.loader import render_to_string

from finance.cash_flow import format_minor
from finance.templatetags.ui import amount_text, amount_tone_class, daisy_widget
from tests.a11y import assert_accessible

MINUS = "−"
MAX_BIGINT = 2**63 - 1


class _SampleForm(forms.Form):
    name = forms.CharField()
    kind = forms.ChoiceField(choices=(("a", "A"), ("b", "B")))
    notes = forms.CharField(widget=forms.Textarea)
    csv_file = forms.FileField(required=False)


@pytest.mark.parametrize(
    ("minor", "currency", "tone", "expected"),
    [
        (124_000, "USD", "", "+$1,240.00"),
        (-124_000, "USD", "", f"{MINUS}$1,240.00"),
        (5, "USD", "", "+$0.05"),
        (0, "USD", "", "$0.00"),
        (124_000, "EUR", "", "+1,240.00 EUR"),
        (-50, "CAD", "", f"{MINUS}0.50 CAD"),
        (0, "EUR", "", "0.00 EUR"),
        (MAX_BIGINT, "USD", "", "+$92,233,720,368,547,758.07"),
        (-MAX_BIGINT - 1, "USD", "", f"{MINUS}$92,233,720,368,547,758.08"),
        (414_000, "USD", "out", f"{MINUS}$4,140.00"),
        (-2_500, "USD", "out", "+$25.00"),
        (0, "USD", "out", "$0.00"),
        (1_200, "USD", "in", "+$12.00"),
    ],
)
def test_amount_text_shows_sign_and_currency(minor, currency, tone, expected):
    assert amount_text(format_minor(minor, currency), tone) == expected


def test_amount_text_treats_negative_zero_as_zero():
    assert amount_text("-0.00 USD") == "$0.00"
    assert amount_text("-0.00 EUR", "out") == "0.00 EUR"
    assert amount_tone_class("-0.00 USD") == "tabular-nums text-right"


def test_amount_text_leaves_other_text_alone():
    for text in ("—", "", "12%", "1.00 usd", None):
        assert amount_text(text) == text


def test_amount_tone_class_uses_tabular_figures_and_neutral_spending():
    assert amount_tone_class("12.00 USD").split() == ["tabular-nums", "text-right", "amount-in"]
    assert "amount-out" in amount_tone_class("-4.00 USD")
    assert "amount-in" not in amount_tone_class("0.00 USD")
    assert "amount-in" in amount_tone_class("-4.00 USD", "in")
    assert amount_tone_class("12.00 USD", "out") == "tabular-nums text-right"
    assert amount_tone_class("-12.00 USD", "out") == "tabular-nums text-right"


def test_amount_include_renders_the_formatted_value():
    html = render_to_string("finance/_amount.html", {"value": "-1,240.00 USD", "tone": ""})
    assert f"{MINUS}$1,240.00" in html


def test_page_header_has_one_h1_helper_more_and_actions():
    html = render_to_string(
        "finance/_page_header.html",
        {
            "title": "Cash flow",
            "helper": "Amounts in USD.",
            "more": "Projected months are estimates.",
            "actions": "finance/_amount.html",
            "value": "1.00 USD",
            "tone": "",
        },
    )
    assert html.count("<h1") == 1
    assert "Amounts in USD." in html
    assert "<summary>More</summary>" in html and "Projected months are estimates." in html
    assert "+$1.00" in html
    assert_accessible(html)
    bare = render_to_string("finance/_page_header.html", {"title": "Budgets"})
    assert "page-header-helper" not in bare and "page-header-actions" not in bare


def test_row_menu_has_no_inline_script_or_style():
    html = render_to_string(
        "finance/_row_menu.html",
        {"label": "Actions for Dining", "items": "finance/_status.html", "word": "Over"},
    )
    assert "<script" not in html and "style=" not in html and " on" not in re.sub(r'"[^"]*"', '""', html)


def test_status_tag_pairs_an_icon_with_a_word():
    html = render_to_string("finance/_status.html", {"word": "Missing import"})
    assert '<svg aria-hidden="true"' in html
    assert "<span>Missing import</span>" in html


def test_daisy_widget_adds_control_classes_and_error_state():
    bound = _SampleForm({"name": "", "kind": "a", "notes": "ok"})
    assert "input-error" in daisy_widget(bound["name"])
    assert "select select-bordered" in daisy_widget(bound["kind"])
    assert "textarea" in daisy_widget(bound["notes"])
    errored = _SampleForm({"name": "x", "kind": "a", "notes": ""})
    assert "textarea-error" in daisy_widget(errored["notes"])
    errored.add_error("csv_file", "Choose a CSV file.")
    assert "file-input-error" in daisy_widget(errored["csv_file"])


def test_form_include_keeps_field_names():
    form = _SampleForm()
    html = Template("{% include 'finance/_form_fields.html' %}").render(Context({"form": form}))
    assert 'name="name"' in html
    assert 'name="kind"' in html
    assert 'for="' in html
    assert "file-input" in html
    assert 'name="csv_file"' in html
