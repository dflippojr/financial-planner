#!/bin/sh
# One Django process for daily sync, batch AI jobs and interactive chat.
set -eu

exec python /app/scripts/background_loop.py
