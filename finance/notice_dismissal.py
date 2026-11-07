"""Dismissing the investment-activity notice, per member (#699).

The notice on Home, Spending and each spending-category page explains that
investment activity is left out of income and spending. A member may hide it;
the choice is stored on their own Person, so it holds on every device and
never changes what another member sees. The stored value is a hash of the
wording they dismissed, so new wording shows the notice again for everyone.
"""

import hashlib

from . import cash_flow


def notice_hash(text=None):
    """SHA-256 hex of the notice wording; the current wording when text is None."""
    if text is None:
        text = cash_flow.INVESTMENT_NOTICE
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_dismissed(person, text=None):
    """True when this member dismissed exactly this wording of the notice."""
    if person is None or not person.investment_notice_dismissed_hash:
        return False
    return person.investment_notice_dismissed_hash == notice_hash(text)


def show_notice(person, report):
    """Whether a page should render the notice for this member's report."""
    if report is None or not getattr(report, "includes_investment", False):
        return False
    return not is_dismissed(person, report.investment_notice)


def dismiss(person, shown_hash):
    """Record the dismissal when shown_hash is the current wording's hash.

    A page rendered before the wording changed sends the old hash; it is
    refused, so the member sees the new wording before hiding it.
    """
    current = notice_hash()
    if shown_hash != current:
        return False
    if person.investment_notice_dismissed_hash != current:
        person.investment_notice_dismissed_hash = current
        person.save(update_fields=("investment_notice_dismissed_hash", "updated_at"))
    return True


def notice_context(user, report):
    """Template context for the notice partial and the page header's More note.

    The More note carries the same explanation whenever investment accounts
    are in scope, dismissed or not, so it stays reachable after dismissal.
    """
    person = getattr(user, "person", None)
    text = getattr(report, "investment_notice", None)
    includes_investment = bool(getattr(report, "includes_investment", False))
    return {
        "show_investment_notice": show_notice(person, report),
        "investment_more_note": text if includes_investment and text else "",
        "investment_notice_hash": notice_hash(text),
    }
