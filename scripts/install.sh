#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DOMAIN=""
IDP_DOMAIN=""
EMAIL=""

usage() {
  echo "Usage: scripts/install.sh --domain <app-domain> [--idp-domain <idp-domain>] [--email <email>]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --idp-domain) IDP_DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "The Docker Compose plugin is required." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to generate .env." >&2
  exit 1
fi
if [ -f .env ]; then
  echo ".env already exists. Use scripts/update.sh to update, or remove .env to reinstall." >&2
  exit 1
fi

if [ -z "$DOMAIN" ]; then
  read -r -p "App domain (for example cygnus.example.com): " DOMAIN
fi
if [ -z "$IDP_DOMAIN" ]; then
  IDP_DOMAIN="idp.$DOMAIN"
  read -r -p "Authentik domain [$IDP_DOMAIN]: " answer
  IDP_DOMAIN="${answer:-$IDP_DOMAIN}"
fi
if [ -z "$EMAIL" ]; then
  read -r -p "Admin/ACME email [admin@example.com]: " EMAIL
  EMAIL="${EMAIL:-admin@example.com}"
fi

if [ -z "$DOMAIN" ]; then
  echo "An app domain is required." >&2
  exit 1
fi

python3 scripts/setup_env.py --domain "$DOMAIN" --idp-domain "$IDP_DOMAIN" --email "$EMAIL"
docker compose config --quiet
docker compose up -d --build --remove-orphans
docker compose ps

cat <<EOF

Started.
  App:       https://$DOMAIN
  Authentik: https://$IDP_DOMAIN

The stack listens only on loopback:
  app:       http://127.0.0.1:8000
  Authentik: http://127.0.0.1:9000

Configure your nginx from deploy/nginx/cygnusforge.conf, then reload it. The app will not
be reachable at the HTTPS URLs until the proxy is configured.

Initial Authentik login: akadmin
Password: grep AUTHENTIK_BOOTSTRAP_PASSWORD .env

After signing in:
  1. Change the Authentik password.
  2. Add akadmin to Directory -> Groups -> cygnusforge-admins -> Users.
  3. Sign in to the app, then open Admin -> Models and add a model.
EOF
