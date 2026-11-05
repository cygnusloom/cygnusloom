#!/bin/sh
set -eu

# The compose stack disables application-side table creation and applies migrations here.
# Docker DNS can briefly lag behind a healthy database on a fresh start, so retry the
# migration connection instead of crash-looping on a transient name-resolution failure.
if [ "${CYGNUSLOOM_AUTO_CREATE_SCHEMA:-0}" != "1" ]; then
  attempt=1
  log=/tmp/cygnusloom-migrate.log
  while ! alembic upgrade head >"$log" 2>&1; do
    if [ "$attempt" -ge 30 ]; then
      echo "database migrations failed after $attempt attempts" >&2
      cat "$log" >&2
      rm -f "$log"
      exit 1
    fi
    echo "database is not ready; retrying migrations ($attempt/30)..." >&2
    attempt=$((attempt + 1))
    sleep 2
  done
  rm -f "$log"
fi

exec uvicorn --factory cygnusloom.web.app:app_from_env \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
  --workers "${WEB_CONCURRENCY:-1}"
