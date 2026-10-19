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
| `tool_call` | `rulesets/<rule_set>/` | `name`: the tool name the GM sees (letters, digits, `_`, `-`; unique in the rule set); `description`: when and how to use it, including each parameter; `parameters`: a JSON schema object (`type` `object`, with `properties` and optional `required`); `python_module`: the id of a `python_module` entry; `function`: the name of a callable in that module; `player_input` (optional): `{"dice": "1d20", "param": "roll"}` pauses the turn so the player rolls the dice, and passes the total to the function as `param`, or the player's free-form reply instead; `details` (optional): a template such as `"Modifiers: {relationship:+d}."` shown with the prompt, filled from the arguments and checked at load (it may only name schema properties); `activity` (optional): a template such as `"You rolled {die}."` shown to the player in the turn log, filled from the arguments and the result. |

A `tool_call`'s parameters must match its function. Every schema property must be a keyword argument of the function, and every argument without a default must be a required property. Load fails otherwise. A tool with `player_input` also needs a required string `prompt` property: the GM writes the question the player sees, such as "Roll to persuade him." Other types are read but not used yet. Within one directory, each `(type, id)` pair must be unique. Content loads at startup, so a bad file stops the app with an error that names the file.

Content is trusted code. A `python_module` imports a Python module, which runs its top-level code. Only add rule sets you would run as Python.

Bundled content:

- `freeform` rule set: no fixed rules beyond keeping the world consistent unless the player asks otherwise. Its `social_roll` tool call asks the player to roll one d20 (the GM writes the prompt), then adds a relationship modifier and a circumstance modifier, which the AI GM decides. The player may type a free-form reply instead. The Python side is [rules/freeform.py](src/cygnusforge/rules/freeform.py) and the dice are in [rules/dice.py](src/cygnusforge/rules/dice.py).
- `gnomeforest` world: a magical mushroom gnome forest.

Tool calls: each message offers the rule set's `tool_call` entries to the model as function tools. When the model calls one, the engine runs the Python function with the model's arguments and returns the result to the model, which then writes its reply. A message may run up to three rounds of tool calls; a fourth request fails the turn. Bad calls (unknown name, invalid JSON, wrong or missing or unexpected arguments, or a function that raises) return an `error` object to the model instead of failing the turn, so it can recover. The transcript keeps the reply and the turn's activity lines (for example "You rolled 14 on the d20..."), but not the raw tool results; those are in the Transcript tab.

Player input: a tool call with `player_input` pauses the turn. The page shows the GM's prompt, its `details` line (such as the modifiers), and a Roll button (or a text box for a free-form reply). The server rolls the dice when you click, so the turn cannot be rerolled. Nothing from a paused turn is saved, and the composer stays disabled until you answer. Continue retries a reply whose narration failed after your answer was stored, without rolling again. The dice use a random generator by default; tests seed it.

Settings are fixed when a campaign is created. Databases from before the player-input change need a new database, because messages gained an `activity` column and there is a new `pending_turn` table. Delete `cygnusforge.db` or point `CYGNUSFORGE_DATABASE_URL` somewhere new.

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
| `CYGNUSFORGE_TEST_LOGIN` | off | Set to `1`, `true`, `yes`, or `on` to show a "Log in as test user" button on the login page. It signs in a fixed `test-user@example.com` account with a random password. For local testing only; do not enable on a shared server |

Token counts are estimates (about four characters per token). The campaign page shows the estimated prompt size next to the settings button. The full transcript is always kept; only the prompt sent to the model is summarized. A summary is asked to stay within about 25% of the window, and if it still runs longer, its oldest text is dropped.

A reply has two phases. In the decision phase the GM decides whether a roll or other tool is needed and makes the tool calls; its text is not shown. In the narration phase the GM writes the reply the player reads, and only that text streams in. A status line shows the current phase (for example "Deciding...", "Running social_roll...", or "Narrating..."). A turn that waits for a player roll pauses between phases, so decisions can continue after the answer. A reply is saved only after the model finishes; if the model fails, nothing is saved and your message stays in the box to resend.

Each AI reply has a gear button. It opens a "Reply details" dialog with two tabs. The Context tab shows exactly what the model received for that reply (the system prompt, rules, tool definitions, the summary, the kept messages, and any tool rounds), with each part labelled by its source. The Transcript tab shows everything that happened in that round: your message, tool calls and their results, and the reply. Each reply stores its own copy of that context and transcript, so long chats use more database space. Replies from before these features show "No context was recorded" or no transcript.

Model ids take the form `provider/model`, for example `ollama/gemma4:e2b` or `openai/gpt-4o-mini` with `CYGNUSFORGE_LLM_OPENAI_BASE_URL` set.

## Scripted fake model (for tests)

`fake/scripted` reads a directive from your message: `__FAKE__:` followed by a strict JSON object (quoted keys). Each user message after the directive uses the next entry in `turns`:

```text
__FAKE__:{"turns": [
  {"text": "Hello."},
  {"rounds": [[{"name": "social_roll", "arguments": {"prompt": "Roll to persuade.", "relationship": 1, "circumstance": 0}}]],
   "text": "You rolled well."},
  {"error": "upstream exploded"}
]}
```

- `rounds` lists the tool calls the GM makes, one list per round. The engine runs them for real, so each name must be a tool in the rule set. Decision calls get these rounds; the narration does not. A `social_roll` call needs a `prompt` argument, and it pauses the turn until the roll (or a text reply) is posted.
- `text` is the narration the player reads.
- `error` makes the turn fail and saves nothing. The failed message is not kept, so resending it retries the same entry until you send a new directive.
- A newer directive replaces the older one. When the script runs out, the model echoes your last message.

## Operations

```sh
uv run python -m cygnusforge.cli reset-password you@example.com
uv run pytest
uv run ruff check
```

Rate limiting is in memory and per process, so run a single worker.
