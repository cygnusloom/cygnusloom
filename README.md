# Cygnus Forge

A minimal web chat over an LLM. It has accounts (email and password), campaigns (named chats), and nothing else. Each message picks its model, so you can switch models mid-campaign.

## Run

```sh
uv sync
# Put the CYGNUSFORGE_* settings from Configuration in .env (gitignored)
uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env --reload
```

Open http://127.0.0.1:8000, register, and create a campaign, then pick a model in the composer before sending. `CYGNUSFORGE_SECRET_KEY` is required, so set it in `.env` or your shell. The default model is `ollama/gemma4:e2b`, so Ollama should be serving at `http://localhost:11434`. To try the app without a model, pick `fake/scripted`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CYGNUSFORGE_SECRET_KEY` | (required) | Signs session cookies |
| `CYGNUSFORGE_DATABASE_URL` | `sqlite:///./cygnusforge.db` | SQLAlchemy URL |
| `CYGNUSFORGE_LLM_PROVIDERS` | `ollama/gemma4:e2b,fake/scripted` | Comma-separated `provider/model` ids offered to users |
| `CYGNUSFORGE_LLM_DEFAULT_MODEL` | `ollama/gemma4:e2b` | Pre-selected model; always offered, even if not in the list above |
| `CYGNUSFORGE_LLM_<NAME>_BASE_URL` | `ollama` has a default | OpenAI-compatible base URL for provider `<name>` |
| `CYGNUSFORGE_LLM_<NAME>_API_KEY` | none | API key for provider `<name>` |
| `CYGNUSFORGE_LOG_LEVEL` | `INFO` | Log level |

Model ids take the form `provider/model`, for example `ollama/gemma4:e2b` or `openai/gpt-4o-mini` with `CYGNUSFORGE_LLM_OPENAI_BASE_URL` set.

## Scripted fake model (for tests)

`fake/scripted` reads a directive from your message: `__FAKE__:` followed by a strict JSON object (quoted keys). Each assistant turn after it consumes the next entry in `turns`:

```text
__FAKE__:{"turns": [
  {"text": "Hello."},
  {"text": "Rolling.", "tool_calls": [{"name": "roll", "arguments": {"sides": 20}, "output": "14"}]},
  {"error": "upstream exploded"}
]}
```

- `text` is the reply text.
- `tool_calls` render as `<tool_call>` and `<tool_output>` tags in the reply.
- `error` makes the turn fail and saves nothing. The same entry is retried until you send a new directive.
- A newer directive replaces the older one. When the script runs out, the model echoes your last message.

## Operations

```sh
uv run python -m cygnusforge.cli reset-password you@example.com
uv run pytest
uv run ruff check
```

Rate limiting is in memory and per process, so run a single worker.
