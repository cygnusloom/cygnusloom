# Authentik setup for Cygnus Loom

The compose stack starts Authentik, its Postgres database, and Redis alongside the app. Cygnus Loom never stores passwords or roles; Authentik owns both.

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
   app on `127.0.0.1:21872` and Authentik on `127.0.0.1:21873`; nginx forwards to those ports.
5. Open `https://<AUTHENTIK_DOMAIN>/` and sign in as `akadmin` with the bootstrap password.
   Change that password in Authentik after signing in; the environment variable is only read on
   the first start.
6. Grant the Cygnus Loom administrator role: **Directory → Groups → cygnusloom-admins →
   Users → Add existing user → akadmin**. A user who is not in this group is a normal player.
7. Open `https://<CYGNUSLOOM_DOMAIN>/login` and continue with the identity provider. You are
   signed in as `akadmin` and see the Admin link. The account page links back to Authentik's
   user interface for changing your password or MFA.
8. Open **Admin → Models** and add at least one language model. Cygnus Loom ships with none,
   so players cannot create campaigns until this is done.

The bundled blueprint creates the `cygnusloom-admins`, `cygnusloom-credit-exempt`, and
`cygnusloom-players` groups, the invitation enrollment flows, the OIDC provider, and the Cygnus
Loom application. Role changes take effect at the next Cygnus Loom sign-in.

The blueprint lives at `deploy/authentik/blueprints/cygnusloom.yaml`. It sets the application's
Launch URL to `CYGNUSLOOM_BASE_URL`, which makes Cygnus Loom appear on the Authentik user
dashboard. It also sets the application icon to
`<CYGNUSLOOM_BASE_URL>/static/brand/cygnus-loom.svg`, which the app serves publicly, so the
dashboard tile uses the Cygnus Loom mark. Check `docker compose logs authentik-worker` if it did
not apply; the blueprint schema can differ between Authentik releases. The manual equivalent is
below.

The blueprint also brands Authentik's default brand and authentication flow, so the sign-in
page shows the Cygnus Loom lockup above the form over a starry night sky, headed “Welcome to
Cygnus Loom!”, instead of the stock Authentik logo and background. The logo, favicon, and
background are served by the app from `/static/brand/`, and the lockup follows Authentik's
light or dark theme. The browser loads those images directly, so they appear only while the app
is reachable. Re-apply the blueprint after upgrading to pick up branding changes on an existing
install.

`scripts/update.sh` re-applies the blueprint after `docker compose up` and retries briefly while
Authentik starts, so an existing installation gains groups and flows added to the blueprint,
such as `cygnusloom-credit-exempt` and its invitation flow. If the apply fails the update only
warns; check **System → Blueprints** for the error or run the command from the upgrade note
below by hand.

The Authentik server and worker wait for Docker DNS to resolve `authentik-db` and
`authentik-redis` before starting. If an older installation still hangs with `Temporary failure
in name resolution`, run `docker compose restart authentik-server authentik-worker` or run
`./scripts/update.sh` again.

If Cygnus Loom is missing from **Applications → Applications**, the blueprint was not applied;
check **System → Blueprints** for the error and the worker logs. If it exists in the admin list
but not on your user dashboard, open the application and set **Launch URL** to the app URL
(`http://localhost:21872` locally, or `https://<CYGNUSLOOM_DOMAIN>` in production).

## Invite-only accounts

The blueprint creates two invitation enrollment flows. `cygnusloom-enrollment-invite` adds
new users to `cygnusloom-players`. `cygnusloom-enrollment-invite-exempt` adds them to
`cygnusloom-credit-exempt` instead, so the campaigns they own use AI replies without spending
credits. Both flows start with an Invitation stage, so no one can create an account without an
invitation. Invitations are single-use by default; create one per player. Clearing **Single
use** makes a reusable signup link until it expires, so set a short expiry and share it only
with the intended group.

1. Apply the blueprint with `./scripts/update.sh` (or the manual command above).
2. In Authentik open **Directory → Invitations → Create**. Give the invitation a name, choose
   **Cygnus Loom invitation enrollment** (`cygnusloom-enrollment-invite`) for a normal player
   or **Cygnus Loom credit-exempt invitation enrollment**
   (`cygnusloom-enrollment-invite-exempt`) to exempt the new account, enable **Single use**
   (recommended), and set an expiry.
3. Copy the generated invitation link. It looks like
   `https://<AUTHENTIK_DOMAIN>/if/flow/cygnusloom-enrollment-invite/?itoken=<token>` (replace
   the flow slug with `cygnusloom-enrollment-invite-exempt` for an exempt invitation). The flow
   redirects to `CYGNUSLOOM_BASE_URL/auth/login` automatically after enrollment. To land on a
   campaign join page instead, append `&next=` with the URL-encoded app login URL, including its
   own `next=/join/<code>` value. The policy only honors a `next` that starts with the app base
   URL.
4. Share the link privately. The player chooses a username, password, name, and email, is added
   to the flow's group (`cygnusloom-players`, or `cygnusloom-credit-exempt` for the exempt
   flow), and is returned to Cygnus Loom. Reusing the link fails.
5. The player can change their email, password, and MFA from the Account page; it links to
   Authentik's user interface. Invited accounts are created as internal Authentik users so that
   page works.

An Authentik user-write stage can assign only one group, so exempt invitees are not added to
`cygnusloom-players`. Cygnus Loom does not require that group; it matters only if you have
bound the application to `cygnusloom-players` with a policy. In that case, bind
`cygnusloom-credit-exempt` to the application as well, or add exempt players to
`cygnusloom-players` after enrollment. Credit exemption covers only campaigns the account
owns, and the role is read at the next Cygnus Loom sign-in.

If you are already signed in to Authentik, open the invitation link in a private window; the
enrollment flow requires an unauthenticated session. If you have added policy bindings to the
Cygnus Loom application, bind `cygnusloom-players` to it so invited players can sign in.

The invite flows do not send a verification email, so they work without SMTP. The single-use
invitation is the proof that the account was invited. If you add a Google or other source to
Authentik, give its source-enrollment flow an Invitation stage too; otherwise the source
bypasses invite-only.

## Add a player manually

Use this when you do not want to issue an invitation link.

1. In Authentik open **Directory → Users → Create**.
2. Set a username, email, and password. Leave the user out of `cygnusloom-admins` for a normal
   player. The username is only for signing in to Authentik; Cygnus Loom gives the account its
   own display name, which the player can change on the Account page. Other players never see
   the Authentik username or email.
3. Give the user the Cygnus Loom application: **Applications → Cygnus Loom → Policy / Group /
   User Bindings → Bind existing user**, or add the user to a group that is already bound.
   Optionally add the user to `cygnusloom-players` so they are grouped with invited players.
4. The user opens `https://<CYGNUSLOOM_DOMAIN>/login` and signs in with that username and
   password.

To make someone an administrator, add them to `cygnusloom-admins` in Authentik. They must sign
in to Cygnus Loom again before the new role is visible.

## Exempt an account from credits

To let an account use AI GM replies without spending credits, add it to
`cygnusloom-credit-exempt`: **Directory → Groups → cygnusloom-credit-exempt → Users → Add
existing user**. The account must sign in to Cygnus Loom again before the group appears in its
stored roles. Only campaigns that account owns are exempt; joining another player's campaign
does not exempt that campaign. A debug campaign can still force charges.

For a new account, create the invitation on `cygnusloom-enrollment-invite-exempt` instead; the
flow adds the user to this group during enrollment.

Existing installations created before this group existed can create it by re-applying the
blueprint:

```sh
docker compose exec -T authentik-server ak apply_blueprint /blueprints/custom/cygnusloom.yaml
```

`scripts/update.sh` runs this command for you after an upgrade, with a short retry loop.

## Local-only server

For a machine with no DNS names, use the HTTP override:

```sh
python3 scripts/setup_env.py --domain localhost --idp-domain idp.localhost
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Open http://localhost:21872. Authentik is at http://idp.localhost:21873. Add `akadmin` to the
administrator group, then add a model under **Admin → Models**. The base compose file publishes
both services on `127.0.0.1`; the local override switches to plain HTTP, turns off secure
cookies, and publishes Authentik on all interfaces so the app container can resolve
`idp.localhost` through Docker's host gateway. Use it for a local machine or a trusted network,
not for an internet-facing deployment.

For a LAN deployment with real hostnames, point both names at the server in local DNS and use
the nginx sample. The identity provider does not need to be reachable from the public internet.

## Manual OIDC provider

If the blueprint does not match your Authentik version, create:

- A confidential OAuth2/OpenID provider with client ID `CYGNUSLOOM_OIDC_CLIENT_ID`, the client
  secret from `.env`, authorization redirect URI `https://<CYGNUSLOOM_DOMAIN>/auth/callback`,
  and logout redirect URI `https://<CYGNUSLOOM_DOMAIN>/login` (select the matching redirect
  URI type for each in Authentik 2026.5 and later).
- Scopes `openid`, `email`, `profile`, and `groups`.
- A scope mapping named `groups` with:

  ```python
  return {"groups": [group.name for group in request.user.groups.all()]}
  ```

- An application bound to the provider.
- The `cygnusloom-players` and `cygnusloom-credit-exempt` groups, prompt stages, Invitation
  stage, and the `cygnusloom-enrollment-invite` and `cygnusloom-enrollment-invite-exempt` flows
  if you want invite-only accounts with optional credit exemption. The bundled blueprint is the
  reference.

Cygnus Loom reads the claim named by `CYGNUSLOOM_OIDC_ADMIN_CLAIM` (default `groups`) and
compares it with `CYGNUSLOOM_OIDC_ADMIN_VALUES` (default `cygnusloom-admins`). It does not
write roles back to Authentik.

