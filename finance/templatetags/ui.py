import json
import re

from django import template
from django.forms.widgets import CheckboxInput, CheckboxSelectMultiple, FileInput, RadioSelect, Select, Textarea


register = template.Library()


@register.filter
def daisy_widget(field):
    widget = field.field.widget
    existing = widget.attrs.get("class", "")
    if isinstance(widget, (CheckboxSelectMultiple, RadioSelect)):
        # Group layout belongs on the container, not the checkbox-sized class.
        existing = " ".join(name for name in existing.split() if name not in {"checkbox", "radio", "input"})
        classes = "form-choices"
    elif isinstance(widget, Select):
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


def _bead_member(context):
    from finance.ai_services import AiError, member_has_ai
    from finance.chat_services import chat_backend_for
    from finance.policy_services import may_use_ai

    request = context.get("request")
    person = getattr(getattr(request, "user", None), "person", None)
    if not person:
        return None
    cache = context.render_context.setdefault("ai_bead_permission", {})
    if person.pk not in cache:
        allowed = may_use_ai(person) and member_has_ai(person)
        if allowed:
            try:
                chat_backend_for(person)
            except AiError:
                allowed = False
        cache[person.pk] = allowed
    if not cache[person.pk]:
        return None
    return person


BEAD_FILTER_FIELDS = {
    "date_from": "date_from", "date_to": "date_to", "month": "month",
    "account": "account_id", "scope": "scope", "category": "category_id",
    "grouping": "grouping", "horizon": "horizon",
}


def _bead_form_filters(kind, values, cleaned):
    from finance.ai_tiles import TILES

    filters = dict(values)
    for source, target in BEAD_FILTER_FIELDS.items():
        if source in cleaned and target in TILES[kind].filters:
            value = cleaned[source]
            filters.setdefault(target, getattr(value, "pk", value))
    return filters


def _bead_date_filters(filters):
    from datetime import date

    for key, value in list(filters.items()):
        if isinstance(value, date):
            filters[key] = value.isoformat()[:7] if key == "month" else value.isoformat()
    return filters


BEAD_IGNORED_FIELDS = {
    "cash-flow-totals": ("grouping", "horizon"),
    "home-budgets": ("date_from", "date_to", "account", "scope", "grouping", "horizon", "tag"),
    "spending-categories": ("grouping",),
    "transaction-totals": ("q", "category", "amount_min", "amount_max", "has_note", "is_split", "set_by"),
}


def _bead_unsupported_filters(form, kind, cleaned):
    from finance.ai_tiles import TILES

    supported = {key for key, target in BEAD_FILTER_FIELDS.items() if target in TILES[kind].filters}
    # Amount mode has no effect without an amount bound, which is excluded below.
    supported.add("amount_mode")
    # These controls affect other tiles on the same page, not this tile.
    supported.update(BEAD_IGNORED_FIELDS.get(kind, ()))
    fields = getattr(form, "fields", cleaned)
    return any(cleaned.get(key) not in (None, "") for key in fields if key not in supported)


def _bead_filters(context, kind, values):
    form = context.get("filter_form")
    if form is not None and form.is_bound and not form.is_valid():
        return None
    cleaned = getattr(form, "cleaned_data", {}) or getattr(form, "initial", {})
    if _bead_unsupported_filters(form, kind, cleaned):
        return None
    if kind in {"cash-flow-chart", "cash-flow-months"} and getattr(context.get("report"), "scenario_changes", ()):
        return None
    filters = _bead_form_filters(kind, values, cleaned)
    if kind in {"transactions", "transaction-totals"}:
        filters.update({key: cleaned.get(key) for key in ("date_from", "date_to")})
        filters["all_dates"] = True
        if kind == "transactions" and cleaned.get("category") in ("transfer", "uncategorized"):
            return None
    return _bead_date_filters(filters)


@register.inclusion_tag("finance/_ai_bead.html", takes_context=True)
def ai_tile(context, kind, *, has_data=False, **values):
    from finance.ai_tiles import TILES, sanitize_tile, tile_data_shareable

    if not has_data or kind not in TILES:
        return {}
    person = _bead_member(context)
    if person is None:
        return {}
    filters = _bead_filters(context, kind, values)
    if filters is None:
        return {}
    tile_context = sanitize_tile({"kind": kind, "filters": filters}, person)
    share_cache = context.render_context.setdefault("ai_bead_sharing", {})
    key = (person.pk, tile_context["filters"].get("account_id"), tile_context["filters"].get("scope"))
    if key not in share_cache:
        share_cache[key] = tile_data_shareable(person, tile_context["filters"])
    if not share_cache[key]:
        return {}
    return {"tile": TILES[kind], "tile_context": json.dumps(tile_context),
            "tile_prompts": json.dumps(TILES[kind].prompts)}
