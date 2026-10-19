# Cygnus Forge

A web chat over an LLM, growing into an RPG. It has accounts (email and password) and campaigns (named chats). Each campaign has a world template and a rule set, both picked when it is created. Each message picks its model, so you can switch models mid-campaign.

## Run

```sh
uv sync
# Put the CYGNUSFORGE_* settings from Configuration in .env (gitignored)
uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env --reload
```

The dev container (`.devcontainer/`) uses the uv image and reads `.devcontainer/devcontainer.env` as its env file. That file is gitignored, so keep your own copy of it.

Open http://127.0.0.1:8000, register, and create a campaign, then pick a model from the settings button beside Send before sending. `CYGNUSFORGE_SECRET_KEY` is required, so set it in `.env` or your shell. The default model is `ollama/gemma4:e2b`, so Ollama should be serving at `http://localhost:11434`. To try the app without a model, pick `fake/scripted`.

## Content: world templates and rule sets

Content lives under `src/cygnusforge/data/`. Each campaign refers to a world template and a rule set by directory name.

```text
data/
  worlds/<world>/           one directory per world template
    world.json
  rulesets/<rule_set>/      one directory per rule set
    rule_set.json           the rule set's metadata and rules
    tool_calls.json         any other files you like; all *.json files are read
```

Every JSON file is a list of objects. Each object has a `type` and an `id`, in the style of Cataclysm-DDA:

```json
[
  {"type": "world", "id": "gnomeforest", "name": "Gnome Forest", "description": "..."}
]
```

The types the engine understands today:

| Type | Where | Fields |
| --- | --- | --- |
| `world` | `worlds/<world>/` | `name`, `description`. Exactly one per world; `id` must equal the directory name. |
| `rule_set` | `rulesets/<rule_set>/` | `name`, `description`. Exactly one per rule set; `id` must equal the directory name. |
| `rule` | `rulesets/<rule_set>/` | `text`. A plain-text rule the AI GM follows. |
| `python_module` | `rulesets/<rule_set>/` | `module`: a dotted import path, imported when the content loads. |
| `tool_call` | `rulesets/<rule_set>/` | `name`: the tool name the GM sees (letters, digits, `_`, `-`; unique in the rule set); `description`: when and how to use it, including each parameter; `parameters`: a JSON schema object (`type` `object`, with `properties` and optional `required`); `python_module`: the id of a `python_module` entry; `function`: the name of a callable in that module. |

A `tool_call`'s parameters must match its function. Every schema property must be a keyword argument of the function, and every argument without a default must be a required property. Load fails otherwise. Other types are read but not used yet. Within one directory, each `(type, id)` pair must be unique. Content loads at startup, so a bad file stops the app with an error that names the file.

Content is trusted code. A `python_module` imports a Python module, which runs its top-level code. Only add rule sets you would run as Python.

Bundled content:

- `freeform` rule set: no fixed rules beyond keeping the world consistent unless the player asks otherwise. Its `social_roll` tool call rolls one d20 plus a relationship modifier and a circumstance modifier, which the AI GM decides. The Python side is [rules/freeform.py](src/cygnusforge/rules/freeform.py).
- `gnomeforest` world: a magical mushroom gnome forest.

Tool calls: each message offers the rule set's `tool_call` entries to the model as function tools. When the model calls one, the engine runs the Python function with the model's arguments and returns the result to the model, which then writes its reply. A message may run up to three rounds of tool calls; a fourth request fails the turn. Bad calls (unknown name, invalid JSON, wrong or missing or unexpected arguments, or a function that raises) return an `error` object to the model instead of failing the turn, so it can recover. Tool calls and their results are not saved in the transcript; only the final reply is.

Settings are fixed when a campaign is created. Campaigns created before these settings existed need a new database, because the schema gained two columns. Delete `cygnusforge.db` or point `CYGNUSFORGE_DATABASE_URL` somewhere new.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CYGNUSFORGE_SECRET_KEY` | (required) | Signs session cookies |
| `CYGNUSFORGE_DATABASE_URL` | `sqlite:///./cygnusforge.db` | SQLAlchemy URL |
| `CYGNUSFORGE_LLM_PROVIDERS` | `ollama/gemma4:e2b,fake/scripted` | Comma-separated `provider/model` ids offered to users |
| `CYGNUSFORGE_LLM_DEFAULT_MODEL` | `ollama/gemma4:e2b` | Pre-selected model; always offered, even if not in the list above |
| `CYGNUSFORGE_LLM_CONTEXT_TOKENS` | `32768` | Context window used to estimate prompt size. At 50% the older chat is summarized, keeping the newest messages worth about 10% of the window verbatim |
| `CYGNUSFORGE_LLM_<NAME>_BASE_URL` | `ollama` has a default | OpenAI-compatible base URL for provider `<name>` |
| `CYGNUSFORGE_LLM_<NAME>_API_KEY` | none | API key for provider `<name>` |
| `CYGNUSFORGE_LOG_LEVEL` | `INFO` | Log level |

Token counts are estimates (about four characters per token). The campaign page shows the estimated prompt size next to the settings button. The full transcript is always kept; only the prompt sent to the model is summarized. A summary is asked to stay within about 25% of the window, and if it still runs longer, its oldest text is dropped.

Each AI reply has a gear button. It opens a Context dialog that shows exactly what the model received for that reply (the system prompt, rules, tool definitions, the summary, the kept messages, and any tool rounds), with each part labelled by its source. Each reply stores its own copy of that context, so long chats use more database space. Replies from before this feature show "No context was recorded".

Model ids take the form `provider/model`, for example `ollama/gemma4:e2b` or `openai/gpt-4o-mini` with `CYGNUSFORGE_LLM_OPENAI_BASE_URL` set.

## Scripted fake model (for tests)

`fake/scripted` reads a directive from your message: `__FAKE__:` followed by a strict JSON object (quoted keys). Each assistant turn after it consumes the next entry in `turns`:

```text
__FAKE__:{"turns": [
  {"text": "Hello."},
  {"text": "Rolling.", "tool_calls": [{"name": "social_roll", "arguments": {"relationship": 1, "circumstance": 0}}]},
  {"text": "You rolled well."},
  {"error": "upstream exploded"}
]}
```

- `text` is the reply text.
- `tool_calls` are requests the engine runs for real, so each name must be a tool in the rule set. Each entry is one model call, so a tool round uses a turn and the narration is the next turn.
- `error` makes the turn fail and saves nothing. The same entry is retried until you send a new directive.
- A newer directive replaces the older one. When the script runs out, the model echoes your last message.

## Operations

```sh
uv run python -m cygnusforge.cli reset-password you@example.com
uv run pytest
uv run ruff check
```

Rate limiting is in memory and per process, so run a single worker.
