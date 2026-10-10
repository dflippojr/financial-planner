import math
import re
from pathlib import Path

import pytest
from django import forms
from django.conf import settings
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
        (414_000, "USD", "spent", f"{MINUS}$4,140.00"),
        (-2_500, "USD", "spent", "+$25.00"),
        (0, "USD", "spent", "$0.00"),
        (1_200, "USD", "out", "+$12.00"),
    ],
)
def test_amount_text_shows_sign_and_currency(minor, currency, tone, expected):
    assert amount_text(format_minor(minor, currency), tone) == expected


def test_amount_text_treats_negative_zero_as_zero():
    assert amount_text("-0.00 USD") == "$0.00"
    assert amount_text("-0.00 EUR", "spent") == "0.00 EUR"
    assert amount_tone_class("-0.00 USD") == "tabular-nums text-right"


def test_amount_text_leaves_other_text_alone():
    for text in ("—", "", "12%", "1.00 usd", None):
        assert amount_text(text) == text


def test_amount_tone_class_uses_tabular_figures_and_neutral_spending():
    assert amount_tone_class("12.00 USD").split() == ["tabular-nums", "text-right", "amount-in"]
    assert "amount-out" in amount_tone_class("-4.00 USD")
    assert "amount-in" not in amount_tone_class("0.00 USD")
    assert "amount-in" in amount_tone_class("-4.00 USD", "in")
    for tone in ("out", "spent"):
        assert amount_tone_class("12.00 USD", tone) == "tabular-nums text-right"
        assert amount_tone_class("-12.00 USD", tone) == "tabular-nums text-right"


def test_amount_include_renders_the_formatted_value():
    html = render_to_string("finance/_amount.html", {"value": "-1,240.00 USD", "tone": ""})
    assert f'<span class="tabular-nums text-right amount-out">{MINUS}$1,240.00</span>' in html


# WCAG contrast of the amount tints, using daisyUI's own light and dark tokens.
STATIC_SRC = Path(settings.BASE_DIR) / "static" / "src"


def _theme(name):
    source = (STATIC_SRC / "vendor" / "daisyui.mjs").read_text(encoding="utf-8")
    body = re.search(r"[{,] " + name + r": \{([^}]*)\}", source).group(1)
    return dict(re.findall(r'"(--color-[a-z0-9-]+)": "([^"]+)"', body))


def _oklch_to_srgb(value):
    lightness, chroma, hue = re.fullmatch(r"oklch\(([\d.]+)% ([\d.]+) ([\d.]+)\)", value).groups()
    lightness, chroma, hue = float(lightness) / 100, float(chroma), math.radians(float(hue))
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(min(max(channel, 0.0), 1.0) for channel in linear)


def _hex_to_linear(value):
    channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)


def _luminance(linear):
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _amount_tints():
    css = (STATIC_SRC / "app.css").read_text(encoding="utf-8")
    light = re.search(r'\[data-theme="light"\] \{([^}]*)\}', css).group(1)
    dark = re.search(r'\[data-theme="dark"\] \{([^}]*)\}', css).group(1)
    return dict(re.findall(r"(--amount-\w+): ([^;]+);", light)), dict(re.findall(r"(--amount-\w+): ([^;]+);", dark))


def test_amount_tints_reach_4_5_to_1_on_page_and_card_surfaces():
    light_tints, dark_tints = _amount_tints()
    light, dark = _theme("light"), _theme("dark")
    for tint in ("--amount-in", "--amount-out"):
        for surface in ("--color-base-100", "--color-base-200"):
            ratio = _contrast(_hex_to_linear(light_tints[tint]), _oklch_to_srgb(light[surface]))
            assert ratio >= 4.5, (tint, "light", surface, round(ratio, 2))
            dark_token = re.fullmatch(r"var\((--color-[a-z-]+)\)", dark_tints[tint]).group(1)
            ratio = _contrast(_oklch_to_srgb(dark[dark_token]), _oklch_to_srgb(dark[surface]))
            assert ratio >= 4.5, (tint, "dark", surface, round(ratio, 2))


def test_stock_light_tints_fail_which_is_why_they_are_overridden():
    light = _theme("light")
    white = _oklch_to_srgb(light["--color-base-100"])
    assert _contrast(_oklch_to_srgb(light["--color-success"]), white) < 4.5
    assert _contrast(_oklch_to_srgb(light["--color-error"]), white) < 4.5


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
    assert '<details class="page-header-more">' in html and "<summary>More</summary>" in html
    assert 'class="page-header-actions"' in html and "+$1.00" in html
    assert_accessible(html)
    bare = render_to_string("finance/_page_header.html", {"title": "Budgets"})
    assert "page-header-helper" not in bare and "page-header-actions" not in bare


def test_row_menu_is_a_named_details_dropdown_without_inline_script():
    html = render_to_string(
        "finance/_row_menu.html",
        {"label": "Actions for Dining", "items": "finance/_status.html", "word": "Over"},
    )
    assert "<details class=\"row-menu dropdown dropdown-end\" data-row-menu>" in html
    assert 'aria-label="Actions for Dining"' in html
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


def test_daisy_widget_file_fields_use_file_input_class():
    html = daisy_widget(_SampleForm()["csv_file"])
    assert "file-input" in html
    assert "file-input-bordered" in html
    assert "file-input-primary" in html
    errored = _SampleForm(data={})
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
