#!/bin/sh
# Background AI job runner. Polls due jobs; never serves HTTP.
set -eu

poll=${AI_JOB_POLL_SECONDS:-15}
echo "AI job runner started with TZ=${TZ:-UTC} and poll seconds: $poll"
exec python /app/scripts/ai_jobs_loop.py
