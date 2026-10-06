"""Chat lane: answers pending chat turns in the background container, never in a web request.

Batch jobs (ai_jobs.py) poll every AI_JOB_POLL_SECONDS, honour the local-model quiet
window and back off between attempts; a single batch job can also hold the job loop for
the whole harness session timeout. Interactive chat must start within about a second,
so it runs on its own lane: a short poll in a separate thread that hands each claimed
turn to a small worker pool. Turns in one conversation run one at a time, oldest
first, because they share one harness session.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from .ai_jobs import _lock_qs
from .ai_types import PROVIDER_ERROR, UNAVAILABLE
from .chat_services import answer_turn, failed_reply, failure_text
from .models import AiConversation, AiConversationMessage

logger = logging.getLogger(__name__)

PENDING = AiConversationMessage.Status.PENDING


def poll_seconds():
    return max(0.2, float(getattr(settings, "AI_CHAT_POLL_SECONDS", 1)))


def worker_count():
    return max(1, int(getattr(settings, "AI_CHAT_WORKERS", 4)))


def stale_seconds():
    """A claimed turn whose runner stopped sending heartbeats this long ago is abandoned."""
    return max(10, int(getattr(settings, "AI_CHAT_STALE_SECONDS", 60)))


def unclaimed_max_age_seconds():
    """With no runner alive, a turn nobody picked up within one full session wait is abandoned too."""
    timeout = int(getattr(settings, "AGENT_HARNESS_SESSION_TIMEOUT_SECONDS", 600))
    margin = int(getattr(settings, "AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS", 120))
    return timeout + margin


def new_token():
    return uuid.uuid4().hex


def claim_next_turn(token, *, now=None):
    """Claim the oldest pending turn in a conversation that has no turn running. Returns its pk."""
    moment = now or timezone.now()
    busy = AiConversationMessage.objects.filter(status=PENDING, claimed_at__isnull=False).values(
        "conversation_id"
    )
    with transaction.atomic():
        candidates = (
            AiConversationMessage.objects.filter(status=PENDING, claimed_at__isnull=True)
            .exclude(conversation_id__in=busy)
            .order_by("pk")
        )
        row = _lock_qs(candidates).first()
        if row is None:
            return None
        claimed = AiConversationMessage.objects.filter(
            pk=row.pk, status=PENDING, claimed_at__isnull=True
        ).update(claim_token=token, claimed_at=moment, heartbeat_at=moment)
        return row.pk if claimed else None


def run_claimed_turn(pk, token, *, sleep=None, monotonic=None):
    """Answer one turn this runner claimed and store the reply. Returns True if it was stored."""
    turn = (
        AiConversationMessage.objects.select_related("conversation__member", "reply_to")
        .filter(pk=pk, status=PENDING, claim_token=token)
        .first()
    )
    if turn is None:
        return False
    try:
        reply = answer_turn(turn, sleep=sleep, monotonic=monotonic)
    except AiConversation.DoesNotExist:
        # The conversation was deleted or expired while the harness answered.
        return False
    except Exception:
        logger.exception("Chat turn %s failed", pk)
        reply = failed_reply(failure_text(PROVIDER_ERROR), backend=turn.backend)
    return _finish(pk, token, reply)


def _finish(pk, token, reply):
    # Only the runner that still holds the claim may answer: a turn already failed
    # as stale keeps its failure rather than flipping back to an answer.
    return bool(
        AiConversationMessage.objects.filter(pk=pk, status=PENDING, claim_token=token).update(**reply)
    )


def heartbeat(token, *, now=None):
    return AiConversationMessage.objects.filter(status=PENDING, claim_token=token).update(
        heartbeat_at=now or timezone.now()
    )


def recover_stale_turns(*, now=None, pk=None):
    """Fail pending turns whose runner died or that no runner picked up. Returns how many."""
    moment = now or timezone.now()
    heartbeat_cutoff = moment - timedelta(seconds=stale_seconds())
    stale = Q(claimed_at__isnull=False, heartbeat_at__lt=heartbeat_cutoff)
    pending = AiConversationMessage.objects.filter(status=PENDING)
    # A turn waiting behind a running turn in its conversation, or for a free worker,
    # is only queued. Unclaimed turns are abandoned only when no runner is alive.
    runner_alive = pending.filter(claimed_at__isnull=False, heartbeat_at__gte=heartbeat_cutoff).exists()
    if not runner_alive:
        stale |= Q(
            claimed_at__isnull=True,
            created_at__lt=moment - timedelta(seconds=unclaimed_max_age_seconds()),
        )
    query = pending.filter(stale)
    if pk is not None:
        query = query.filter(pk=pk)
    return query.update(
        status=AiConversationMessage.Status.FAILED,
        role=AiConversationMessage.Role.ERROR,
        content=failure_text(UNAVAILABLE),
    )


def process_pending_turns(*, sleep=None, monotonic=None):
    """Answer every pending turn in this thread, one after another. Returns how many were stored."""
    token = new_token()
    stored = 0
    while True:
        pk = claim_next_turn(token)
        if pk is None:
            return stored
        if run_claimed_turn(pk, token, sleep=sleep, monotonic=monotonic):
            stored += 1


class ChatLane:
    """Polls for pending turns and runs each on a worker thread, up to worker_count() at once."""

    def __init__(self, *, executor=None, workers=None):
        self.token = new_token()
        self.workers = workers or worker_count()
        # Pool threads open their own database connections, so they close them after each turn.
        self._pool_threads = executor is None
        self.executor = executor or ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="chat-turn")
        self._inflight = set()
        self._lock = threading.Lock()

    def tick(self, *, now=None):
        moment = now or timezone.now()
        heartbeat(self.token, now=moment)
        recover_stale_turns(now=moment)
        while self._busy() < self.workers:
            pk = claim_next_turn(self.token, now=moment)
            if pk is None:
                break
            with self._lock:
                self._inflight.add(pk)
            self.executor.submit(self._run, pk)

    def _busy(self):
        with self._lock:
            return len(self._inflight)

    def _run(self, pk):
        try:
            run_claimed_turn(pk, self.token)
        except Exception:
            logger.exception("Chat turn %s could not be stored", pk)
        finally:
            with self._lock:
                self._inflight.discard(pk)
            if self._pool_threads:
                connection.close()

    def run_forever(self, stop_event):
        logger.info("Chat lane started with %s workers", self.workers)
        while not stop_event.is_set():
            close_old_connections()
            try:
                self.tick()
            except Exception:
                logger.exception("Chat lane poll failed")
            stop_event.wait(poll_seconds())
