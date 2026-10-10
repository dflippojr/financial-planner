import re

from django import template
from django.forms.widgets import CheckboxInput, CheckboxSelectMultiple, FileInput, Select, Textarea


register = template.Library()


@register.filter
def daisy_widget(field):
    widget = field.field.widget
    existing = widget.attrs.get("class", "")
    if isinstance(widget, Select):
        classes = "select select-bordered w-full"
        if field.errors:
            classes += " select-error"
    elif isinstance(widget, Textarea):
        classes = "textarea textarea-bordered w-full"
        if field.errors:
            classes += " textarea-error"
    elif isinstance(widget, FileInput):
        classes = "file-input file-input-bordered file-input-primary w-full"
        if field.errors:
            classes += " file-input-error"
    elif isinstance(widget, CheckboxInput):
        classes = "checkbox"
    elif isinstance(widget, CheckboxSelectMultiple):
        classes = "checkbox"
    else:
        classes = "input input-bordered w-full"
        if field.errors:
            classes += " input-error"
    merged = f"{existing} {classes}".strip()
    return field.as_widget(attrs={**widget.attrs, "class": merged})


# The "1,240.00 USD" shape every *_display money string uses (cash_flow.format_minor and the models).
_AMOUNT_DISPLAY = re.compile(r"^(-?)(\d[\d,]*)\.(\d+) ([A-Z]{3})$")
MINUS_SIGN = "\N{MINUS SIGN}"


@register.filter
def amount_text(display, tone=""):
    """Show a money display string as "+$1,240.00", "-$1,240.00" with U+2212, or "+1,240.00 EUR".

    Zero has no sign. tone "out" marks an outflow total (spending, a charge, a liability)
    whose positive value is money out, so its sign flips. Other text is left alone.
    """
    match = _AMOUNT_DISPLAY.match(str(display).strip())
    if not match:
        return display
    minus, whole, fraction, currency = match.groups()
    negative = bool(minus) != (tone == "out")
    if _is_zero(whole, fraction):
        sign = ""
    else:
        sign = MINUS_SIGN if negative else "+"
    number = f"{whole}.{fraction}"
    if currency == "USD":
        return f"{sign}${number}"
    return f"{sign}{number} {currency}"


@register.filter
def amount_tone_class(display, tone=""):
    # Spending is neutral text: the explicit sign, not colour, says which way money moved.
    if tone == "out":
        choice = ""
    else:
        choice = tone or _tone_from_display(display)
    if choice == "in":
        return "tabular-nums text-right amount-in"
    if choice == "out":
        return "tabular-nums text-right amount-out"
    return "tabular-nums text-right"


@register.filter
def lookup(mapping, key):
    if not mapping:
        return None
    return mapping.get(key)


def _tone_from_display(display):
    match = _AMOUNT_DISPLAY.match(str(display).strip())
    if match and _is_zero(match.group(2), match.group(3)):
        return ""
    text = str(display).lstrip()
    if text.startswith("-"):
        return "out"
    if text.startswith(("0.00", "0,00")):
        return ""
    return "in"


def _is_zero(whole, fraction):
    return not any(digit in "123456789" for digit in whole + fraction)
