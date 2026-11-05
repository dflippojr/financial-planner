"""Form fields for amounts people type (#657).

Every dollar amount a member types goes through MoneyField, so `10,000`,
`$10,000.50` and `$ 10,000` mean the same everywhere. Percent fields take a
trailing `%`. Both keep Django's own min/max, max_digits and decimal_places
checks, which run on the normalized number.
"""

import re
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

# One sign at most, before or after the dollar sign; spaces only after `$`.
# The number is ASCII digits either in valid 3-digit comma groups or with no
# commas at all, then an optional fraction. Decimal() alone would also take
# `1e5`, `1_000`, `NaN`, `Infinity` and non-ASCII digits, so this pattern is
# the whole grammar.
_MONEY = re.compile(
    r"(?P<lead>[-+]?)(?:\$\s*)?(?P<inner>[-+]?)"
    r"(?P<number>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]*)?|[0-9]+(?:\.[0-9]*)?|\.[0-9]+)",
    re.ASCII,
)
_PERCENT_SUFFIX = re.compile(r"\s*%\Z")


def normalize_money_text(text):
    """Return `text` as a plain decimal string, or None if it is not an amount."""
    match = _MONEY.fullmatch(text.strip())
    if match is None or (match["lead"] and match["inner"]):
        return None
    sign = "-" if "-" in (match["lead"] + match["inner"]) else ""
    return sign + match["number"].replace(",", "")


class MoneyField(forms.DecimalField):
    """A dollar amount: accepts `10000`, `10,000`, `$10,000.50`, `-$25`, `$-25`."""

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip()
            if value:
                normalized = normalize_money_text(value)
                if normalized is None:
                    raise ValidationError(self.error_messages["invalid"], code="invalid")
                value = normalized
        return super().to_python(value)

    def prepare_value(self, value):
        # Render stored amounts in plain fixed-point form (never `1E+3`), so an
        # edit form always shows a value this field accepts back unchanged.
        if isinstance(value, Decimal) and value.is_finite():
            return format(value, "f")
        return super().prepare_value(value)


def _strip_percent(value):
    if isinstance(value, str):
        return _PERCENT_SUFFIX.sub("", value.strip())
    return value


class PercentField(forms.DecimalField):
    """A decimal percent such as an APR: `19.99`, ` 19.99 `, `19.99%`."""

    def to_python(self, value):
        return super().to_python(_strip_percent(value))


class WholePercentField(forms.IntegerField):
    """A whole-number percent: `10`, ` 10 `, `10%`."""

    def to_python(self, value):
        return super().to_python(_strip_percent(value))
