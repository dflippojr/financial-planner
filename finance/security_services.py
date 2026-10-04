from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.http import Http404
from django.utils import timezone

from .auth_services import normalize_username
from .models import MemberSecurityEvent, MemberSession, Person


SECURITY_EVENT_RETENTION_DAYS = 90
USER_AGENT_MAX_LENGTH = 200
SESSION_ACTIVITY_MIN_INTERVAL = timedelta(minutes=1)

EVENT_TYPES = MemberSecurityEvent.EventType


def client_ip(request):
    if request is None:
        return ""
    return (request.META.get("REMOTE_ADDR") or "")[:45]


def client_user_agent(request):
    if request is None:
        return ""
    return (request.META.get("HTTP_USER_AGENT") or "")[:USER_AGENT_MAX_LENGTH]


def _person_from(member):
    if member is None:
        return None
    if isinstance(member, Person):
        person = member
    else:
        try:
            person = member.person
        except (AttributeError, Person.DoesNotExist):
            return None
    if person is None or person.pk is None:
        return None
    return person


def record_security_event(member, event_type, *, request=None, ip_address="", user_agent=""):
    person = _person_from(member)
    if person is None:
        return None
    return MemberSecurityEvent.objects.create(
        member=person,
        event_type=event_type,
        ip_address=ip_address or client_ip(request),
        user_agent=user_agent or client_user_agent(request),
    )


def record_sign_in_failure_for_username(username, request):
    normalized = normalize_username(username or "")
    if not normalized:
        return None
    user = get_user_model().objects.filter(username=normalized).select_related("person").first()
    person = _person_from(user)
    if person is None:
        return None
    return record_security_event(person, EVENT_TYPES.SIGN_IN_FAILURE, request=request)


def events_for(principal):
    return MemberSecurityEvent.objects.visible_to(principal).order_by("-occurred_at", "-pk")


def active_sessions_for(principal, *, now=None):
    now = now or timezone.now()
    live_keys = Session.objects.filter(expire_date__gte=now).values("session_key")
    return (
        MemberSession.objects.visible_to(principal)
        .filter(session_key__in=live_keys)
        .order_by("-last_activity_at", "-pk")
    )


def touch_member_session(request, *, force=False):
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    person = _person_from(user)
    if person is None:
        return None
    session_key = request.session.session_key
    if not session_key:
        request.session.save()
        session_key = request.session.session_key
    if not session_key:
        return None
    ip_address = client_ip(request)
    user_agent = client_user_agent(request)
    now = timezone.now()
    row, created = MemberSession.objects.get_or_create(
        session_key=session_key,
        defaults={
            "member": person,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "created_at": now,
            "last_activity_at": now,
        },
    )
    if row.member_id != person.pk:
        return row
    if created:
        return row
    stale = force or (now - row.last_activity_at) >= SESSION_ACTIVITY_MIN_INTERVAL
    changed = row.ip_address != ip_address or row.user_agent != user_agent
    if not stale and not changed:
        return row
    row.ip_address = ip_address
    row.user_agent = user_agent
    row.last_activity_at = now
    row.save(update_fields=("ip_address", "user_agent", "last_activity_at"))
    return row


def drop_session_index(session_key):
    if not session_key:
        return 0
    deleted, _detail = MemberSession.objects.filter(session_key=session_key).delete()
    return deleted


def retouch_after_session_cycle(request, previous_key):
    if previous_key and previous_key != request.session.session_key:
        drop_session_index(previous_key)
    return touch_member_session(request, force=True)


def _delete_django_sessions(session_keys):
    keys = [key for key in session_keys if key]
    if not keys:
        return
    Session.objects.filter(session_key__in=keys).delete()
    MemberSession.objects.filter(session_key__in=keys).delete()


def revoke_indexed_sessions_for_user(user):
    try:
        person = user.person
    except (AttributeError, Person.DoesNotExist):
        return
    if person is None or person.pk is None:
        return
    keys = list(MemberSession.objects.filter(member_id=person.pk).values_list("session_key", flat=True))
    _delete_django_sessions(keys)


def revoke_session_for(principal, session_id, *, current_session_key=None):
    row = MemberSession.objects.visible_to(principal).filter(pk=session_id).first()
    if row is None:
        raise Http404()
    was_current = bool(current_session_key) and row.session_key == current_session_key
    _delete_django_sessions([row.session_key])
    if not was_current:
        record_security_event(principal, EVENT_TYPES.SIGN_OUT)
    return was_current


def revoke_other_sessions_for(principal, current_session_key):
    rows = list(MemberSession.objects.visible_to(principal).exclude(session_key=current_session_key or ""))
    _delete_django_sessions([row.session_key for row in rows])
    if rows:
        record_security_event(principal, EVENT_TYPES.SIGN_OUT)
    return len(rows)


def purge_old_security_events(*, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=SECURITY_EVENT_RETENTION_DAYS)
    deleted, _detail = MemberSecurityEvent.objects.filter(occurred_at__lt=cutoff).delete()
    return deleted


def purge_stale_member_sessions(*, now=None):
    now = now or timezone.now()
    live_keys = Session.objects.filter(expire_date__gte=now).values("session_key")
    deleted, _detail = MemberSession.objects.exclude(session_key__in=live_keys).delete()
    return deleted
