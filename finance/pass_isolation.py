"""Keep one failing step or member from stopping the rest of a scheduled pass.

Logs name the step or member id and the error type only: a message could quote
financial values. Database-unavailable errors still propagate, so the
background process exits and Docker restarts it.
"""

from django.db import InterfaceError, OperationalError, connection, transaction


def database_unavailable(exc):
    """True for a lost connection; False for a deadlock or lock timeout on a live one.

    PostgreSQL reports both kinds as OperationalError, so probe the connection.
    """
    if isinstance(exc, InterfaceError):
        return True
    if not isinstance(exc, OperationalError):
        return False
    try:
        return not connection.is_usable()
    except Exception:  # No connection to probe.
        return True


class MemberFailures(Exception):
    """Some members failed; every other member's work was done and kept."""

    def __init__(self, label, count):
        super().__init__(f"{label} failed for {count} member(s)")


def for_each_member(people, work, *, label, logger):
    """Run work(person) for each person in its own transaction and return the results.

    A failure rolls back only that member's work and the loop moves on. After
    every member has run, raises MemberFailures if any failed, so the caller
    can record the run as failed.
    """
    results = []
    failures = 0
    for person in people:
        try:
            with transaction.atomic():
                results.append(work(person))
        except Exception as exc:  # The pass must reach every member.
            if database_unavailable(exc):
                raise
            failures += 1
            logger.error("%s for member %s failed (%s).", label, person.pk, type(exc).__name__)
    if failures:
        raise MemberFailures(label, failures)
    return results


def run_step(name, function, *, failed, logger):
    """Run one step of a pass; on failure log it, add its name to failed and return None."""
    try:
        return function()
    except Exception as exc:  # Later steps must still run.
        if database_unavailable(exc):
            raise
        logger.error("Daily alert pass step %s failed (%s).", name, type(exc).__name__)
        failed.append(name)
        return None
