#!/bin/sh
set -eu

# Docker's embedded DNS can briefly lag behind a healthy database after the network is
# recreated. Wait for the service names before handing off to the Authentik entrypoint.
# If DNS never recovers, exit so the container restart policy gets a fresh network namespace.
wait_for_host() {
  host="$1"
  attempt=1
  while ! python -c "import socket; socket.gethostbyname('$host')" >/dev/null 2>&1; do
    if [ "$attempt" -ge 30 ]; then
      echo "could not resolve $host after $attempt attempts" >&2
      exit 1
    fi
    echo "waiting for Docker DNS to resolve $host ($attempt/30)..." >&2
    attempt=$((attempt + 1))
    sleep 2
  done
}

wait_for_host authentik-db
wait_for_host authentik-redis

exec dumb-init -- ak "$@"
