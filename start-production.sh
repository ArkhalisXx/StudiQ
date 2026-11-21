#!/bin/sh
set -eu
cd "$(dirname "$0")"

export STUDIQ_INSTANCE_DIR="${STUDIQ_INSTANCE_DIR:-/data}"
if [ -n "${RAILWAY_ENVIRONMENT_ID:-}" ] && [ "${RAILWAY_VOLUME_MOUNT_PATH:-}" != "$STUDIQ_INSTANCE_DIR" ]; then
  printf '%s\n' 'Attach a Railway volume at the STUDIQ_INSTANCE_DIR path before starting StudiQ.' >&2
  exit 1
fi

# Railway's public HTTPS endpoint forwards requests to this server.
# Trust only the configured proxy addresses; localhost remains the default.
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
