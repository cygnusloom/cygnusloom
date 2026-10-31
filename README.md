# Cygnus Forge

Cygnus Forge is a web-based RPG with an AI game master. Create a campaign, choose a world and rule set, and play through chat with characters, maps, and a journal.

## Local development

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and OpenSSL:

```sh
uv sync
export CYGNUSFORGE_LLM_DEEPSEEK_API_KEY="<your-key>"  # from platform.deepseek.com
CYGNUSFORGE_SECRET_KEY="$(openssl rand -hex 32)" \
CYGNUSFORGE_TEST_LOGIN=1 \
CYGNUSFORGE_TEST_LOGIN_ADMIN=1 \
CYGNUSFORGE_LLM_PROVIDERS=deepseek/deepseek-chat \
CYGNUSFORGE_LLM_DEFAULT_MODEL=deepseek/deepseek-chat \
CYGNUSFORGE_LLM_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1 \
uv run uvicorn --factory cygnusforge.web.app:app_from_env --reload
```

Open http://127.0.0.1:8000, use the development login, and create a campaign. These variables seed a fresh database only; the admin panel is authoritative afterwards, so manage existing models under **Admin > Models**. Keep a stable `CYGNUSFORGE_SECRET_KEY` across restarts: it encrypts model API keys.

Run checks with `uv run pytest -n auto` and `uv run ruff check`.

## Using DeepSeek

DeepSeek serves an OpenAI-compatible chat completions API, so it plugs in as a normal provider. Create an API key at [platform.deepseek.com](https://platform.deepseek.com) and export it in your shell (or put the export in your shell profile); never commit your key to the repository or paste it into [.env.example](.env.example).

- `deepseek-chat` is the general chat model; `deepseek-reasoner` is the reasoning model. Switch by changing `CYGNUSFORGE_LLM_PROVIDERS` and `CYGNUSFORGE_LLM_DEFAULT_MODEL`.
- The base URL is always `https://api.deepseek.com/v1`; `CYGNUSFORGE_LLM_DEEPSEEK_API_KEY` and `CYGNUSFORGE_LLM_DEEPSEEK_BASE_URL` follow the `CYGNUSFORGE_LLM_<NAME>_*` convention for any OpenAI-compatible provider.
- For the Docker deployment, add the same model under **Admin > Models** after signing in; the API key is stored encrypted with `CYGNUSFORGE_SECRET_KEY` in the database, not in `.env`.

## Docker deployment

Requires Docker Compose, Python 3, two hostnames, and an HTTPS reverse proxy. The stack includes Postgres and Authentik, but not a language model or TLS proxy.

```sh
./scripts/install.sh \
  --domain forge.example.com \
  --idp-domain idp.example.com \
  --email you@example.com
```

Configure nginx for both hostnames using the [nginx guide](deploy/nginx/README.md). Sign in to Authentik as `akadmin` using the bootstrap password in `.env`, add the account to `cygnusforge-admins`, then sign in to Cygnus Forge and add a model under **Admin > Models**. See the [Authentik guide](deploy/authentik/README.md) for user setup and troubleshooting.

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

Configuration examples are in [.env.example](.env.example). Worlds and rule sets live in [src/cygnusforge/data](src/cygnusforge/data); treat content as trusted code because it can import Python modules.

## Content credits

- Cairn 2e by Yochai Gal, licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): https://cairnrpg.com/second-edition/.
- Worlds Without Number by Kevin Crawford (Sine Nomine Publishing, 2021), free edition: https://www.sinenomine-pub.com/.
- Creative tables from the Ironsworn Rulebook by Shawn Tomkin, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), via the Datasworn transcription by rsek: https://ironswornrpg.com.
- The Twin Lakes world uses content from the CAS-1 module *Trouble in Twin Lakes*.
