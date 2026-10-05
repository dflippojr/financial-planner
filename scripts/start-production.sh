#!/bin/sh
set -eu

python manage.py migrate --noinput

# The default access log records the full request line and the Referer header,
# including query strings. Transaction search text travels in the query string
# (/transactions/?q=...), so log only the method, path, status, size, and time.
# --no-control-socket: the app user has no writable home, and nothing uses
# gunicorn's control interface.
# Chat waits inside the request for the Agent Harness session, for up to
# AGENT_HARNESS_SESSION_TIMEOUT_SECONDS (default 600). With gunicorn's default
# 30 s timeout the worker was killed mid-answer, so the timeout must stay above
# that wait. Threads keep one long chat from blocking every other page.
exec gunicorn financial_planner.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --worker-class gthread \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-660}" \
  --access-logfile - \
  --access-logformat '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(L)s' \
  --error-logfile - \
  --capture-output \
  --no-control-socket
