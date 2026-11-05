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
