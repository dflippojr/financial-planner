import re
from pathlib import Path

from django.utils import timezone

from finance.reauth import RECENT_AUTH_SESSION_KEY

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "compose.yml"


def compose_service(name):
    """The text of one compose.yml service block, from its key to the next service."""
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    # From the start of a line, so a depends_on entry with the same name is skipped.
    start = re.search(rf"^  {re.escape(name)}:\n", compose, re.M).start()
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", compose[start + 1 :])
    return compose[start : start + 1 + following.start()] if following else compose[start:]


def stamp_recent_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()
    return client
