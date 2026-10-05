#!/usr/bin/env python
"""Poll and run due AI background jobs and chat turns. Does not serve HTTP."""

import logging
import os
import sys
import threading
import time
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_planner.settings")
django.setup()

from django.conf import settings

from finance.ai_jobs import process_due_jobs
from finance.chat_runner import ChatLane

logger = logging.getLogger(__name__)


def tick():
    try:
        process_due_jobs()
    except Exception:
        logger.exception("AI job poll failed")


def start_chat_lane():
    # Chat turns get their own thread: a batch job can hold this loop for a whole session wait.
    lane = ChatLane()
    thread = threading.Thread(target=lane.run_forever, args=(threading.Event(),), name="chat-lane", daemon=True)
    thread.start()
    return thread


def main():
    poll = max(1, int(getattr(settings, "AI_JOB_POLL_SECONDS", 15)))
    start_chat_lane()
    while True:
        tick()
        time.sleep(poll)


if __name__ == "__main__":
    main()
