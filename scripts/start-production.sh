#!/bin/sh
set -eu

python manage.py migrate --noinput

exec gunicorn financial_planner.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --access-logfile - \
  --error-logfile - \
  --capture-output
