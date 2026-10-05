#!/bin/sh
# Background AI job runner and chat lane. Polls due jobs and chat turns; never serves HTTP.
set -eu

poll=${AI_JOB_POLL_SECONDS:-15}
echo "AI job runner started with TZ=${TZ:-UTC} and poll seconds: $poll (chat: ${AI_CHAT_POLL_SECONDS:-1})"
exec python /app/scripts/ai_jobs_loop.py
