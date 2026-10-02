from django.utils import timezone

from finance.reauth import RECENT_AUTH_SESSION_KEY


def stamp_recent_auth(client):
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()
    return client
