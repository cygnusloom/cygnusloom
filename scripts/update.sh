#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DO_PULL=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) DO_PULL=0; shift ;;
    -h|--help)
      echo "Usage: scripts/update.sh [--no-pull]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f .env ]; then
  echo ".env not found; run scripts/install.sh first." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "The Docker Compose plugin is required." >&2
  exit 1
fi

./scripts/backup.sh

if [ "$DO_PULL" -eq 1 ]; then
  if [ -d .git ]; then
    git pull --ff-only
  else
    echo "No .git directory; skipping git pull."
  fi
fi

docker compose pull
docker compose up -d --build --remove-orphans

# Apply the bundled blueprint explicitly so groups added to it since the last install (such as
# cygnusloom-credit-exempt) exist immediately, instead of waiting for Authentik's own scan.
# Authentik may still be starting, so retry for about a minute; failure only warns and never
# aborts the update.
blueprint_applied=0
for _attempt in {1..30}; do
  if docker compose exec -T authentik-server ak apply_blueprint /blueprints/custom/cygnusloom.yaml; then
    blueprint_applied=1
    break
  fi
  sleep 2
done
if [ "$blueprint_applied" -eq 0 ]; then
  echo "Warning: could not apply the Authentik blueprint; new groups may be missing. Check docker compose logs authentik-server." >&2
fi

docker compose ps

APP_PORT="$(sed -n 's/^CYGNUSLOOM_APP_PORT=//p' .env | head -n 1)"
APP_PORT="${APP_PORT:-21872}"

echo "Local health check:"
curl -fsS "http://127.0.0.1:$APP_PORT/healthz" ||
  echo "App is still starting; check docker compose logs app."

DOMAIN="$(sed -n 's/^CYGNUSLOOM_DOMAIN=//p' .env | head -n 1)"
if [ -n "$DOMAIN" ]; then
  echo "Public health check:"
  curl -fsS "https://$DOMAIN/healthz" ||
    echo "The app is not reachable through nginx yet; check the proxy configuration."
fi

echo "Update complete. Database migrations run automatically on app start."
