#!/bin/sh
set -eu

# The compose stack disables application-side table creation and applies migrations here.
if [ "${CYGNUSFORGE_AUTO_CREATE_SCHEMA:-0}" != "1" ]; then
  alembic upgrade head
fi

exec uvicorn --factory cygnusforge.web.app:app_from_env \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
  --workers "${WEB_CONCURRENCY:-1}"
