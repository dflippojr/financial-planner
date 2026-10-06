"""Independent background lanes sharing one Django process.

Keep the batch poll separate from chat and the daily pass: a harness session can
hold a batch job for ten minutes. The entrypoint supervises all three lanes.
"""

import logging
import os
import threading
import time

from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections, connections
from django.utils import timezone

from .ai_jobs import process_due_jobs
from .chat_runner import ChatLane
from .simplefin_schedule import next_scheduled_sync, parse_five_field_cron, seconds_until

logger = logging.getLogger(__name__)


def run_ai_jobs(stop_event):
    poll = max(1, int(settings.AI_JOB_POLL_SECONDS))
    while not stop_event.is_set():
        close_old_connections()
        try:
            process_due_jobs()
        except Exception:
            logger.exception("AI job poll failed")
        finally:
            connections.close_all()
        stop_event.wait(poll)


def run_daily_pass(stop_event):
    expression = settings.SIMPLEFIN_SYNC_CRON
    due = next_scheduled_sync(expression)
    while not stop_event.is_set():
        wait = seconds_until(due, timezone.localtime())
        if wait > 0:
            stop_event.wait(wait)
            continue
        close_old_connections()
        try:
            call_command("sync_simplefin")
        finally:
            connections.close_all()
        due = next_scheduled_sync(expression, due)


def supervise(threads):
    while all(thread.is_alive() for thread in threads):
        time.sleep(1)
    logger.error("Background lane stopped; exiting so Docker restarts every lane")
    # Chat's executor may still be waiting for a remote session. A normal Python
    # exit joins that pool, leaving an Up container with a dead daily/job lane.
    os._exit(1)


def main():
    # Invalid cron fails before any threads or chat executor are started.
    parse_five_field_cron(settings.SIMPLEFIN_SYNC_CRON)
    stop_event = threading.Event()
    lane = ChatLane()
    threads = [
        threading.Thread(target=target, args=(stop_event,), name=name, daemon=True)
        for name, target in (
            ("ai-jobs", run_ai_jobs),
            ("daily-pass", run_daily_pass),
            ("chat-lane", lane.run_forever),
        )
    ]
    for thread in threads:
        thread.start()
    logger.info("Background runner started: AI jobs, chat lane, daily pass")
    supervise(threads)
