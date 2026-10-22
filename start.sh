#!/bin/sh
set -eu
exec gunicorn app:app --bind "0.0.0.0:${PORT:-10000}" --workers 2 --threads 2 --access-logfile -
