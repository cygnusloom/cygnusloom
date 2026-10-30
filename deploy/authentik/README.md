# Authentik setup for Cygnus Forge

The compose stack starts Authentik, its Postgres database, and Redis alongside the app. Cygnus
Forge never stores passwords or roles; Authentik owns both.

This setup uses **local Authentik accounts only**. There are no Google or Microsoft sources to
configure. Users sign in with a username and password stored in Authentik.

## First start

1. Run `python3 scripts/setup_env.py --domain <app-host> --idp-domain <idp-host> --email <you@example.com>`.
   It writes `.env` with generated secrets. Without Python, copy `.env.example` to `.env` and
   replace every `change-me` value with `openssl rand -hex 32` output.
2. The two domain names must point at this host, and your nginx must terminate TLS for both.
   See [../nginx/README.md](../nginx/README.md) for a sample config and certificate steps.
3. Set `AUTHENTIK_BOOTSTRAP_EMAIL` and `AUTHENTIK_BOOTSTRAP_PASSWORD`. These create the initial
   `akadmin` account on the first start and skip the Authentik setup wizard.
4. Run `./scripts/install.sh ...` or `docker compose up -d --build`. The stack publishes the
   app on `127.0.0.1:8000` and Authentik on `127.0.0.1:9000`; nginx forwards to those ports.
5. Open `https://<AUTHENTIK_DOMAIN>/` and sign in as `akadmin` with the bootstrap password.
   Change that password in Authentik after signing in; the environment variable is only read on
   the first start.
6. Grant the Cygnus Forge administrator role: **Directory → Groups → cygnusforge-admins →
   Users → Add existing user → akadmin**. A user who is not in this group is a normal player.
7. Open `https://<CYGNUSFORGE_DOMAIN>/login` and continue with the identity provider. You are
   signed in as `akadmin` and see the Admin link.
8. Open **Admin → Models** and add at least one language model. Cygnus Forge ships with none,
   so players cannot create campaigns until this is done.

The bundled blueprint creates the `cygnusforge-admins` group, the OIDC provider, and the Cygnus
Forge application. Role changes take effect at the next Cygnus Forge sign-in.

The blueprint lives at `deploy/authentik/blueprints/cygnusforge.yaml`. Check
`docker compose logs authentik-worker` if it did not apply; the blueprint schema can differ
between Authentik releases. The manual equivalent is below.

## Add a player

1. In Authentik open **Directory → Users → Create**.
2. Set a username, email, and password. Leave the user out of `cygnusforge-admins` for a normal
   player.
3. Give the user the Cygnus Forge application: **Applications → Cygnus Forge → Policy / Group /
   User Bindings → Bind existing user**, or add the user to a group that is already bound.
4. The user opens `https://<CYGNUSFORGE_DOMAIN>/login` and signs in with that username and
   password.

To make someone an administrator, add them to `cygnusforge-admins` in Authentik. They must sign
in to Cygnus Forge again before the new role is visible.

## Local-only server

For a machine with no DNS names, use the HTTP override:

```sh
python3 scripts/setup_env.py --domain localhost --idp-domain idp.localhost
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Open http://localhost:8000. Authentik is at http://idp.localhost:9000. Add `akadmin` to the
administrator group, then add a model under **Admin → Models**. The base compose file publishes
both services on `127.0.0.1`; the local override switches to plain HTTP and turns off secure
cookies. Use it for a local machine or a trusted network, not for an internet-facing deployment.

For a LAN deployment with real hostnames, point both names at the server in local DNS and use
the nginx sample. The identity provider does not need to be reachable from the public internet.

## Manual OIDC provider

If the blueprint does not match your Authentik version, create:

- A confidential OAuth2/OpenID provider with client ID `CYGNUSFORGE_OIDC_CLIENT_ID`, the client
  secret from `.env`, authorization redirect URI `https://<CYGNUSFORGE_DOMAIN>/auth/callback`,
  and logout redirect URI `https://<CYGNUSFORGE_DOMAIN>/login` (select the matching redirect
  URI type for each in Authentik 2026.5 and later).
- Scopes `openid`, `email`, `profile`, and `groups`.
- A scope mapping named `groups` with:

  ```python
  return {"groups": [group.name for group in request.user.groups.all()]}
  ```

- An application bound to the provider.

Cygnus Forge reads the claim named by `CYGNUSFORGE_OIDC_ADMIN_CLAIM` (default `groups`) and
compares it with `CYGNUSFORGE_OIDC_ADMIN_VALUES` (default `cygnusforge-admins`). It does not
write roles back to Authentik.

