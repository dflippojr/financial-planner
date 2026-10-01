from django import template
from django.forms.widgets import CheckboxInput, FileInput, Select, Textarea


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
        classes = "file-input file-input-bordered w-full"
        if field.errors:
            classes += " file-input-error"
    elif isinstance(widget, CheckboxInput):
        classes = "checkbox"
    else:
        classes = "input input-bordered w-full"
        if field.errors:
            classes += " input-error"
    merged = f"{existing} {classes}".strip()
    return field.as_widget(attrs={**widget.attrs, "class": merged})


@register.filter
def amount_tone_class(display, tone=""):
    choice = tone or _tone_from_display(display)
    if choice == "in":
        return "tabular-amount text-right amount-in"
    if choice == "out":
        return "tabular-amount text-right amount-out"
    return "tabular-amount text-right"


def _tone_from_display(display):
    text = str(display).lstrip()
    if text.startswith("-"):
        return "out"
    if text.startswith("0.00") or text.startswith("0,00"):
        return ""
    return "in"
