# Authentik setup for Cygnus Forge

The compose stack starts Authentik, its Postgres database, and Redis alongside the app. Cygnus
Forge never stores passwords or roles; Authentik owns both.

## First start

1. Copy `.env.example` to `.env` and replace every `change-me` value. Generate the three secrets
   with `openssl rand -hex 32`.
2. Set `CYGNUSFORGE_DOMAIN` and `AUTHENTIK_DOMAIN` to DNS names that point at this host. Caddy
   obtains certificates automatically. Google and Microsoft require public HTTPS callback URLs,
   so a real domain is needed for social sign-in.
3. Run `docker compose up -d --build`.
4. Open `https://<AUTHENTIK_DOMAIN>/if/flow/initial-setup/` and create the Authentik
   administrator account.
5. The bundled blueprint creates the `cygnusforge-admins` group, the OIDC provider, and the
   Cygnus Forge application. Add your own Authentik user to that group to become a Cygnus Forge
   administrator. Role changes take effect at the next Cygnus Forge sign-in.
6. Open `https://<CYGNUSFORGE_DOMAIN>/login` and continue with the identity provider.

The blueprint lives at `deploy/authentik/blueprints/cygnusforge.yaml`. Check
`docker compose logs authentik-worker` if it did not apply; the blueprint schema can differ
between Authentik releases. The manual equivalent is below.

## Add Google sign-in

1. In Google Cloud Console, create an OAuth 2.0 Web application client. Add the redirect URI that
   Authentik shows for the source, normally
   `https://<AUTHENTIK_DOMAIN>/source/oauth/callback/google/`.
2. In Authentik: **Directory → Federation & Social login → Create → Google OAuth Source**. Set
   the slug, consumer key (Google client ID), and consumer secret.
3. Add the source to the **default-authentication-flow**: **Flows and Stages → Flows →
   default-authentication-flow → Stage Bindings → default-authentication-identification**, then
   select the Google source. It now appears on the Authentik login page.
4. Sign in with Google once. Authentik creates the user. Add that user to
   `cygnusforge-admins` if they should administer Cygnus Forge.

## Add Microsoft sign-in

1. In Microsoft Entra admin center, register a web application. Add the redirect URI Authentik
   shows, normally `https://<AUTHENTIK_DOMAIN>/source/oauth/callback/microsoft/`, and create a
   client secret.
2. In Authentik: **Directory → Federation & Social login → Create → Microsoft OAuth Source**.
   Enter the application (client) ID, client secret, and tenant.
3. Add the source to the same identification stage as above.
4. Sign in once, then assign the group in Authentik.

## Local accounts

Authentik can also hold local accounts. Create them under **Directory → Users** and assign the
administrator group. This is useful when social sign-in is not configured or when the deployment
is not reachable from the public internet.

## Manual OIDC provider

If the blueprint does not match your Authentik version, create:

- A confidential OAuth2/OpenID provider with client ID `CYGNUSFORGE_OIDC_CLIENT_ID`, the client
  secret from `.env`, and redirect URI `https://<CYGNUSFORGE_DOMAIN>/auth/callback`.
- Scopes `openid`, `email`, `profile`, and `groups`.
- A scope mapping named `groups` with:

  ```python
  return {"groups": [group.name for group in user.ak_groups.all()]}
  ```

- An application bound to the provider.

Cygnus Forge reads the claim named by `CYGNUSFORGE_OIDC_ADMIN_CLAIM` (default `groups`) and
compares it with `CYGNUSFORGE_OIDC_ADMIN_VALUES` (default `cygnusforge-admins`). It does not
write roles back to Authentik.
