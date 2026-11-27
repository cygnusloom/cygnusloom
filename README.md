# Cygnus Loom

[Cygnus Loom](https://cygnusloom.com) is a web-based RPG with an AI game master.

You can play pre-made adventures, upload your own campaign modules, or have the AI GM generate a world for you.

Cygnus Loom supports several rulesets, including the basic [Worlds Without Number](https://www.drivethrurpg.com/en/product/348809/worlds-without-number-free-edition) rules and [SRD 5.2.1 (5E compatible)](https://www.dndbeyond.com/srd) rules.

Campaigns use a combination of random table generation, AI GM creativity, and injected human-written modules and scenarios to ensure that each campaign is unique and varied, avoiding AI regression to the average.

## How to Play

There's a few options to play Cygnus Loom.

### On your computer
You can use the Cygnus Loom desktop app, which bundles every part of Cygnus Loom into an easy to use executable. Download `CygnusLoom.exe` (Windows) or `CygnusLoom` (Linux) from
[Releases](https://github.com/cygnusloom/cygnusloom/releases), run it, and follow the setup
wizard.

You will need an AI model to plug in to the AI GM harness. We recommend [DeepSeek Flash](https://platform.deepseek.com), but any OpenAI-compatible model will work; including locally hosted models.

### On a server, anywhere
Currently there is no official server hosting Cygnus Loom. Playing on a server is as simple as connecting to a hosted server. You can play from your PC or any mobile device. You can either connect from your browser, or connect from within the Cygnus Loom desktop app.

On a server you can share a link to your campaign, and other people on the same server that you invite can join your campaign. They'll see the same world and campaign, experience the same AI GM, and you can play the story out together.

When you play on a server, the server owner provides the AI models, and play is pay-as-you-go using credit balances that can be topped-up. Cygnus Loom tries to optimize AI use, so the AI GM is as inexpensive as possible.

#### Hosting a server
To host a server, check out the code and use the docker configuration for the default Authentik + Cygnus Loom setup:

You need Docker with the Compose plugin and two hostnames pointing at the server: one for Cygnus Loom and one for Authentik. nginx terminates TLS in front of both.

```sh
git clone https://github.com/cygnusloom/cygnusloom.git
cd cygnusloom
./scripts/install.sh --domain cygnusloom.example.com
```

The installer asks for the Authentik hostname and an admin email, writes `.env` with generated
secrets, and starts the stack on `127.0.0.1:21872` (app) and `127.0.0.1:21873` (Authentik). Copy
`deploy/nginx/cygnusloom.conf` into your nginx configuration, replace the hostnames and
certificate paths, and reload nginx.

For a machine with no DNS names, use the HTTP-only override instead:

```sh
python3 scripts/setup_env.py --domain localhost --idp-domain idp.localhost
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Initial setup for the admin username and password you will use:

Sign in to Authentik as `akadmin`, using `AUTHENTIK_BOOTSTRAP_PASSWORD` from `.env`, and change
the password. Add `akadmin` to the `cygnusloom-admins` group, then sign in at
`https://<your-app-domain>/login` and add at least one model under **Admin → Models**.

In the hosted server, there are a few special roles you can assign in Authentik:

* `cygnusloom-admins` — administrators: manage models, credits, and accounts.
* `cygnusloom-credit-exempt` — campaigns they own use AI replies without spending credits.
* `cygnusloom-players` — ordinary players, including everyone who joins through an invite link.

To backup or upgrade your Cygnus Loom server:

```sh
./scripts/backup.sh   # dumps both databases into backups/
./scripts/update.sh   # backs up, pulls, rebuilds, restarts; migrations run on start
```

### Exporting and Importing Campaigns
Campaigns are portable between servers or between your own computer and any other server. You can export campaigns from within Cygnus Loom and import them in another Cygnus Loom instance, retaining all the play history, world information, characters, and other campaign details.

## Develop

Contributions are always welcome! To get started building and running Cygnus Loom locally you will need [uv](https://docs.astral.sh/uv/).

You can launch a local server:

```sh
uv sync
export CYGNUSLOOM_LLM_DEEPSEEK_API_KEY="<your-key>"  # from platform.deepseek.com
CYGNUSLOOM_SECRET_KEY="$(openssl rand -hex 32)" \
CYGNUSLOOM_TEST_LOGIN=1 \
CYGNUSLOOM_TEST_LOGIN_ADMIN=1 \
CYGNUSLOOM_LLM_PROVIDERS=deepseek/deepseek-flash \
CYGNUSLOOM_LLM_DEFAULT_MODEL=deepseek/deepseek-flash \
CYGNUSLOOM_LLM_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1 \
uv run uvicorn --factory cygnusloom.web.app:app_from_env --reload
```

or the local desktop app:

```sh
uv run --extra desktop cygnusloom play
```

### Unit tests and formatting

Run the suite through its own runner, which sizes the worker pool to this machine and sets the
required test settings:

```sh
uv run python scripts/test.py
```

Format changed files with `uv run ruff format <paths>`. CI runs `uv run ruff check .`.

## License

Cygnus Loom is Copyright (C) 2026 Cygnus Loom contributors and is free software under the
[GNU Affero General Public License, version 3 or later](LICENSE). It comes with no warranty.

## Content credits

The rule sets and worlds bundled with the engine keep their own licenses; [CREDITS.md](CREDITS.md)
lists them. The app serves the same list on its `/license` page.

## Human written README

This README file shall be written by humans. AI agents shall not change this file unless explicitly directed.