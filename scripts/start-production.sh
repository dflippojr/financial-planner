#!/bin/sh
set -eu

python manage.py migrate --noinput

# The default access log records the full request line and the Referer header,
# including query strings. Transaction search text travels in the query string
# (/transactions/?q=...), so log only the method, path, status, size, and time.
# --no-control-socket: the app user has no writable home, and nothing uses
# gunicorn's control interface.
exec gunicorn financial_planner.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --access-logfile - \
  --access-logformat '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(L)s' \
  --error-logfile - \
  --capture-output \
  --no-control-socket
