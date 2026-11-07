# Cygnus Loom

[Cygnus Loom](https://cygnusloom.com) is a web-based RPG with an AI game master. Create a campaign, choose a world and rule set, and play through chat with characters, maps, and a journal.

## Play the single-player desktop edition

Download `CygnusLoom.exe` (Windows) or `CygnusLoom` (Linux) from
[Releases](https://github.com/cygnusloom/cygnusloom/releases), run it, and follow the setup
wizard. Everything runs on your machine: the app creates a local SQLite database, signs you in
as the local administrator, and asks for one language model. There is no identity provider,
Docker, or Python install.

- Data lives in `%LOCALAPPDATA%\CygnusLoom` on Windows and
  `${XDG_DATA_HOME:-~/.local/share}/cygnusloom` on Linux.
- The wizard offers free local models (Ollama, LM Studio), hosted providers (DeepSeek, OpenAI,
  OpenRouter), any other OpenAI-compatible endpoint, and a scripted demo.
- Run it from a checkout with `uv run --extra desktop cygnusloom play`.
- Build the one-file app with `uv sync --group build` and
  `uv run pyinstaller packaging/cygnusloom.spec`. The
  [desktop workflow](.github/workflows/desktop.yml) builds both platforms on version tags.
- The Linux binary needs glibc 2.35 or newer (Ubuntu 22.04 and later). QtWebEngine is bundled,
  so no system webview or browser is required.

Campaigns can be exported to a single `.json` file from the campaign list or the campaign's
settings dialog, and imported on another machine or server. The file contains the campaign
configuration and its complete event log, including characters, maps, notes, objects, the
journal, and any pending turn. Import creates a new campaign for the signed-in account; if the
exported model is not configured on the target server, the default model is used instead.

## Play with other people

A campaign belongs to one account, and the owner can share it from the Players tab. Each share
creates a join link; the owner can deactivate links at any time, and players who already joined
stay at the table. Every player makes their own character, and the AI GM sees everyone who
joins and brings them into the story. Out of combat, any player can send the next message. In
an encounter, a player acts for their own character on that character's turn, and only that
player can answer its prompts. Every player's page follows the campaign live through a
server-sent event stream, so a turn one player starts appears for the whole table without a
reload. The owner can remove a player and choose whether that player's character leaves the
story or stays as a shared, unclaimed part.

## Credits

Each account has a credit balance. An administrator sets a flat price per AI GM reply on every
model under **Admin > Models**, and the campaign owner pays that price when a reply is saved.
The price is shown in the campaign's model picker. Administrators and the single-player account
are exempt, and a debug campaign can force charges to test the flow. Payment is not integrated:
an administrator grants credits by setting an account's balance on its admin page.

The models page also shows measured token use per model (input, cached input, output, reasoning,
and estimated calls) so an administrator can choose a fair reply price. A provider that does not
report usage is estimated at about four characters per token. Streamed usage requests can be
turned off per model for OpenAI-compatible servers that reject the `stream_options` field.

## Local development

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and OpenSSL:

```sh
uv sync
export CYGNUSLOOM_LLM_DEEPSEEK_API_KEY="<your-key>"  # from platform.deepseek.com
CYGNUSLOOM_SECRET_KEY="$(openssl rand -hex 32)" \
CYGNUSLOOM_TEST_LOGIN=1 \
CYGNUSLOOM_TEST_LOGIN_ADMIN=1 \
CYGNUSLOOM_LLM_PROVIDERS=deepseek/deepseek-chat \
CYGNUSLOOM_LLM_DEFAULT_MODEL=deepseek/deepseek-chat \
CYGNUSLOOM_LLM_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1 \
uv run uvicorn --factory cygnusloom.web.app:app_from_env --reload
```

Open http://127.0.0.1:8000, use the development login, and create a campaign. These variables seed a fresh database only; the admin panel is authoritative afterwards, so manage existing models under **Admin > Models**. Keep a stable `CYGNUSLOOM_SECRET_KEY` across restarts: it encrypts model API keys.

Run checks with `uv run python scripts/test.py` and `uv run ruff check`. The test runner sizes
its worker pool from the CPU and memory free at the moment, never takes more than half of
either (swap excluded), and enforces those ceilings where the operating system allows it. A
bare `pytest` run is refused unless `CYGNUSLOOM_TEST_SETTINGS=1` is set; CI sets that marker and
runs `pytest -n auto` without limits because its runner is dedicated to the job.

## Using DeepSeek

DeepSeek serves an OpenAI-compatible chat completions API, so it plugs in as a normal provider. Create an API key at [platform.deepseek.com](https://platform.deepseek.com) and export it in your shell (or put the export in your shell profile); never commit your key to the repository or paste it into [.env.example](.env.example).

- `deepseek-flash` is the usual model. Switch by changing `CYGNUSLOOM_LLM_PROVIDERS` and `CYGNUSLOOM_LLM_DEFAULT_MODEL`.
- The base URL is always `https://api.deepseek.com/v1`; `CYGNUSLOOM_LLM_DEEPSEEK_API_KEY` and `CYGNUSLOOM_LLM_DEEPSEEK_BASE_URL` follow the `CYGNUSLOOM_LLM_<NAME>_*` convention for any OpenAI-compatible provider.
- For the Docker deployment, add the same model under **Admin > Models** after signing in; the API key is stored encrypted with `CYGNUSLOOM_SECRET_KEY` in the database, not in `.env`.

## Docker deployment

Requires Docker Compose, Python 3, two hostnames, and an HTTPS reverse proxy. The stack includes Postgres and Authentik, but not a language model or TLS proxy.

```sh
./scripts/install.sh \
  --domain cygnusloom.example.com \
  --idp-domain idp.example.com \
  --email you@example.com
```

Configure nginx for both hostnames using the [nginx guide](deploy/nginx/README.md). Sign in to Authentik as `akadmin` using the bootstrap password in `.env`, add the account to `cygnusloom-admins`, then sign in to Cygnus Loom and add a model under **Admin > Models**. See the [Authentik guide](deploy/authentik/README.md) for user setup and troubleshooting.

For a local Docker test without DNS or TLS:

```sh
python3 scripts/setup_env.py --domain localhost --idp-domain idp.localhost
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Open http://localhost:21872 and follow the same Authentik and model setup. This HTTP setup is for a local machine or trusted network only; the override exposes Authentik on all interfaces.

## Maintenance

- `./scripts/update.sh` backs up, updates, rebuilds, and checks the deployment; use `--no-pull` when deploying copied files.
- `./scripts/backup.sh` backs up the databases to `backups/`.
- Never run `docker compose down -v` unless you intend to delete the database volumes.

Configuration examples are in [.env.example](.env.example). Worlds and rule sets live in [src/cygnusloom/data](src/cygnusloom/data); treat content as trusted code because it can import Python modules.

## License

Cygnus Loom is Copyright (C) 2026 Cygnus Loom contributors and is free software under the
[GNU Affero General Public License, version 3 or later](LICENSE). It comes with no warranty.

The AGPL covers network use: a modified version offered to others over a network must offer them
its Corresponding Source. The app shows these terms, with a link to the source, in the footer of
every page and on its `/license` page, and serves the license text itself at `/license.txt`, so a
deployment needs no other host for it. A fork that serves its own code should point the `Source`
entry under `[project.urls]` in `pyproject.toml` at that code.

## Content credits

The rule sets and worlds bundled with the engine keep their own licenses; [CREDITS.md](CREDITS.md)
lists them. The app serves the same list on its `/license` page.
