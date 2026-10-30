#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo ".env not found; run scripts/install.sh first." >&2
  exit 1
fi

mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"

backup() {
  local service="$1" user="$2" database="$3" output="backups/$3-$STAMP.sql"
  if [ -z "$(docker compose ps --quiet "$service")" ]; then
    echo "Skipping $service backup: container is not running."
    return 0
  fi
  echo "Backing up $database -> $output"
  if ! docker compose exec -T "$service" pg_dump -U "$user" "$database" > "$output"; then
    rm -f "$output"
    echo "Backup of $database failed." >&2
    exit 1
  fi
}

backup db cygnusforge cygnusforge
backup authentik-db authentik authentik
