"""One shared budget for SimpleFIN /accounts requests per connection (#694).

SimpleFIN Bridge's developer guide expects "24 requests or fewer per day" per
Access Token, with "a little leeway", quotas "replenished throughout the
day", warnings past the limit and a disabled token after that. Every
/accounts request this app makes (the scheduled sync, Sync now, saving links,
loading the account list, and the Fix and Add checks) is recorded here first,
in a rolling 24-hour log on the connection.

The app caps itself at DAILY_REQUEST_CAP, four under SimpleFIN's 24, so clock
differences or a request SimpleFIN counts that the app cannot see never reach
the warning level. Requests a member starts stop at INTERACTIVE_LIMIT, which
keeps SCHEDULED_RESERVE requests for the scheduled sync. The scheduled sync is
recorded but never refused: missing a day's import costs more than one request.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from finance.models import SimpleFinConnection
from finance.simplefin_errors import SimpleFinError

DAILY_REQUEST_CAP = 20
SCHEDULED_RESERVE = 2
INTERACTIVE_LIMIT = DAILY_REQUEST_CAP - SCHEDULED_RESERVE
WINDOW = timedelta(hours=24)

BUDGET_USED = (
    "SimpleFIN's daily request budget for this connection is used up. "
    "The next scheduled sync will pick up any changes."
)


class SimpleFinBudgetExceeded(SimpleFinError):
    """A member-started request would pass the connection's daily budget."""


def _recent(log, now) -> list[str]:
    since = now - WINDOW
    kept = []
    for stamp in log if isinstance(log, list) else []:
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if since < when <= now + timedelta(minutes=5):
            kept.append(stamp)
    return kept


def requests_used(connection, now=None) -> int:
    """Requests recorded for this connection in the last 24 hours."""
    return len(_recent(connection.request_log, now or timezone.now()))


def interactive_requests_left(connection, now=None) -> int:
    return max(0, INTERACTIVE_LIMIT - requests_used(connection, now))


def reserve_request(connection_id, *, scheduled: bool) -> None:
    """Record one /accounts request before it is sent, or refuse it.

    Runs in its own short transaction with the connection row locked, so two
    requests can never both take the last slot. Attempts count, not
    successes: SimpleFIN counts a request that later fails.
    """
    with transaction.atomic():
        connection = SimpleFinConnection.objects.select_for_update().filter(pk=connection_id).first()
        if connection is None:
            return
        now = timezone.now()
        log = _recent(connection.request_log, now)
        if not scheduled and len(log) >= INTERACTIVE_LIMIT:
            raise SimpleFinBudgetExceeded(BUDGET_USED)
        log.append(now.isoformat())
        SimpleFinConnection.objects.filter(pk=connection_id).update(request_log=log)
