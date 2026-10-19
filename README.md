# Cygnus Forge

A web chat over an LLM, growing into an RPG. It has accounts (email and password) and campaigns (named chats). Each campaign has a world template, a rule set, and a model, all picked when it is created. The model can be changed later from the campaign's settings, and the next message uses the new model. Campaigns also have structured role-play: locations, characters (the player character and NPCs), and notes.

## Run

```sh
uv sync
# Put the CYGNUSFORGE_* settings from Configuration in .env (gitignored)
uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env --reload
```

The dev container (`.devcontainer/`) uses the uv image and reads `.devcontainer/devcontainer.env` as its env file. That file is gitignored, so keep your own copy of it.

Open http://127.0.0.1:8000, register, and create a campaign. To change its model, use the Model settings button beside Send; the campaign header shows the current model. `CYGNUSFORGE_SECRET_KEY` is required, so set it in `.env` or your shell. The default model is `ollama/gemma4:e2b`, so Ollama should be serving at `http://localhost:11434`. To try the app without a model, pick `fake/scripted`.

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
| `world` | `worlds/<world>/` | `name` and `description`. Exactly one per world; `id` must equal the directory name. A world holds no locations or NPCs by default; the AI GM creates them. |
| `location` | `worlds/<world>/` | `name`, `description`. A place in the world. Locations and characters share one namespace, so no two may have the same name (ignoring case). |
| `npc` | `worlds/<world>/` | `name`, `location` (a location id), `description`. Optional `personality`, `accent`, and `quirks` (a list). Any trait left out is picked at random from the core tables. |
| `table` | `rulesets/<rule_set>/` | `options`: a list of strings to pick from. The `core` rule set must declare the tables `personality`, `accent`, and `quirk`; any rule set may replace them. |
| `stat` | `rulesets/<rule_set>/` | `name`, `formula` (dice such as `3d6`). Optional `swap_group`: stats in one group may swap one pair of rolled values at creation. |
| `feature` | `rulesets/<rule_set>/` | `name`, `feature_type` (`item`, `trait`, `spell`, or `feat`). Optional `description` and `bulky` (true or false). |
| `template` | `rulesets/<rule_set>/` | `kind` (a group such as `class` or `background`; the player picks one per kind), `name`, `description`. Optional `features` (a list of `{"feature": id, "quantity": "1" or a dice formula}`), `tables` (table ids rolled for the character), and `quote`. |
| `character_field` | `rulesets/<rule_set>/` | `name`, `description`. A text field when it has no `table`; otherwise a choice from that table. |
| `rule_set` | `rulesets/<rule_set>/` | `name`, `description`. Exactly one per rule set; `id` must equal the directory name. |
| `rule` | `rulesets/<rule_set>/` | `text`. A plain-text rule the AI GM follows. |
| `python_module` | `rulesets/<rule_set>/` | `module`: a dotted import path, imported when the content loads. |
| `tool_call` | `rulesets/<rule_set>/` | `name`: the tool name the GM sees (letters, digits, `_`, `-`; unique in the rule set); `description`: when and how to use it, including each parameter; `parameters`: a JSON schema object (`type` `object`, with `properties` and optional `required`); `python_module`: the id of a `python_module` entry; `function`: the name of a callable in that module; `player_input` (optional): `{"dice": "1d20", "param": "roll"}` pauses the turn so the player rolls the dice, and passes the total to the function as `param`, or the player's free-form reply instead; `details` (optional): a template such as `"Modifiers: {relationship:+d}."` shown with the prompt, filled from the arguments and checked at load (it may only name schema properties); `activity` (optional): a template such as `"You rolled {die}."` shown to the player in the turn log, filled from the arguments and the result. |

A `tool_call`'s parameters must match its function. Every schema property must be a keyword argument of the function, and every argument without a default must be a required property. Load fails otherwise. A tool with `player_input` also needs a required string `prompt` property: the GM writes the question the player sees, such as "Roll to persuade him." Other types are read but not used yet. Within one directory, each `(type, id)` pair must be unique. Content loads at startup, so a bad file stops the app with an error that names the file.

Character options: a rule set describes its characters with `stat`, `feature`, `template`, and `character_field` entries. A template groups features and tables under a kind, and the player picks one template per kind (or leaves it to be rolled). Every part is optional, so an NPC can be given any combination; anything left out is rolled or left blank. The engine builds a JSON schema from these entries (`character_schema` in [content/character.py](src/cygnusforge/content/character.py)), and the GM's `create_npc` tool and the creation form both check their input against it. A rule set that defines no stats, templates, or features gets a schema with only the fields.

Content is trusted code. A `python_module` imports a Python module, which runs its top-level code. Only add rule sets you would run as Python.

Bundled content:

Every campaign uses the `core` rule set, whatever its rule set. It is merged under the selected one: a definition in a rule set replaces the core definition with the same type and id, and a rule set can add its own. The core rule set defines the role-play rules, the role-play tools, and the NPC trait tables.

- `core` rule set (loaded for every campaign): the role-play rules, the tools `create_location`, `update_location`, `delete_location`, `create_npc`, `update_npc`, `delete_npc`, `move_actor`, `add_note`, `update_note`, `delete_note`, `search_notes`, and `speak`, the tables `personality`, `accent`, and `quirk`, and the character fields `appearance` and `biography`, which every rule set has. It is not selectable on its own.
- `cairn2e` rule set: Cairn 2e (https://cairnrpg.com/second-edition/). Stats Strength, Dexterity, and Willpower are rolled 3d6 and may swap one pair within the group, and Hit Protection is 1d6. The 20 backgrounds are the templates. They grant items (petty or bulky), a Backpack, gold, and gear, and roll their own tables. The character fields (physique, skin, hair, face, speech, clothing, virtue, vice, bond, and omen) are rolled from tables. Backgrounds are structure only: they have no mechanical effects yet. The content is taken verbatim from cairnrpg.com by a generator script that asserts each description and table entry matches the source. The game was written by Yochai Gal and is licensed CC BY-SA 4.0.
- `freeform` rule set: no fixed rules beyond keeping the world consistent unless the player asks otherwise. Its `social_roll` tool call asks the player to roll one d20 (the GM writes the prompt), then adds a relationship modifier and a circumstance modifier, which the AI GM decides. The player may type a free-form reply instead. The Python side is [rules/freeform.py](src/cygnusforge/rules/freeform.py) and the dice are in [rules/dice.py](src/cygnusforge/rules/dice.py).
- `gnomeforest` world: a magical mushroom gnome forest. It starts the player character in Mossbell Hollow, with the locations Glowcap Ring and Spore Bridge, and the NPCs Mara Thistledown (with a fixed personality, accent, and quirks), Brin Gallowcap, and Old Hob (both with random traits).
- `kilharbor` world: a cold, ancient forest harbor, where the PCs arrive by ship to seek treasure and knowledge. It has no pre-generated locations or NPCs; the AI GM creates everything. Use it with the `cairn2e` rule set.

Tool calls: each message offers the rule set's `tool_call` entries to the model as function tools. When the model calls one, the engine runs the Python function with the model's arguments and returns the result to the model, which then writes its reply. A message may run any number of rounds of tool calls, and nothing caps them: a model that never stops calling tools keeps the reply open until you discard it. Bad calls (unknown name, invalid JSON, wrong or missing or unexpected arguments, or a function that raises) return an `error` object to the model instead of failing the turn, so it can recover. The transcript keeps the chat lines. The turn's tool activity (for example "You rolled 14 on the d20...") goes into the chat as a small log line, and the raw tool results are in the Transcript tab.

Player input: a tool call with `player_input` pauses the turn. The page shows the GM's prompt, its `details` line (such as the modifiers), and a Roll button (or a text box for a free-form reply). The server rolls the dice when you click, so the turn cannot be rerolled. The prompt is saved with the turn, so it survives a page reload or a restart, and the composer stays disabled until you answer. Your answer is saved before the reply continues, so a later failure never rolls the dice again. The dice use a random generator by default; tests seed it.

Character creation: a new campaign opens on a creation form, and the chat stays closed until the player character exists. The form is built from the rule set's character options. The player's stats are rolled when the form opens (Reroll stats rolls them again), and a swappable stat can take any rolled value from its group. The player picks a template for each kind, the table choices, and the text fields, and leaves any of them as Random. The engine resolves the rest at creation. The PC starts unplaced, and the GM places it with `move_actor` on its first turn.

Structured role-play: every campaign has a player character (the PC), created as described above. The PC starts unplaced, and the GM places it with `move_actor`. Once placed, the PC is an actor like an NPC, and every placed actor is at exactly one location. The world's locations and NPCs are created with the campaign. The GM manages the world with tools: it can create, update, and delete locations (`create_location`, `update_location`, `delete_location`) and NPCs (`create_npc`, `update_npc`, `delete_npc`), move an actor (`move_actor`), and write, update, and delete notes (`add_note`, `update_note`, `delete_note`, by note id). A location with someone in it cannot be deleted until they move, and the player character cannot be updated as an NPC or deleted. Notes are the GM's own record of events, updates, and situations, so it stays consistent; they are not written for the player, and the narration never reveals them. A note is campaign-wide, or attached to a location or an actor; deleting a location or NPC keeps the notes about it. The GM finds notes with `search_notes`, which matches every word of the query against the note's text and subject, newest first, and returns each note's id. The campaign page shows the characters, locations, and notes live. Names must be unique across locations and characters, and a character name cannot match an NPC already in the campaign.

NPC traits (personality, accent, and quirks) are picked at random from the core tables when the NPC does not have them. They are not part of the GM's prompt; they reach the model only through speech.

Speech: the GM does not write dialogue in its narration. To have an NPC speak, it calls `speak` with the NPC's name and the intent. The engine then makes a separate speech step with the same model as the turn. That step gets the NPC's details (who they are, personality, accent, quirks, and location) and the intent, and returns the dialogue. The GM's narration then uses the dialogue. Each line of dialogue is saved as soon as it is written, so it survives a failed narration; the narration is saved when the reply finishes. An NPC can only speak when it is at the PC's location; otherwise the tool returns an error and the GM can recover.

Settings are fixed when a campaign is created. The schema changed, so databases from earlier versions need a new one: delete `cygnusforge.db` or point `CYGNUSFORGE_DATABASE_URL` somewhere new.

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

A reply has two phases. In the state management phase the GM decides what the action changes and what needs to be said or done, and makes the tool calls that change the world, notes, and NPC speech; its text is not shown. In the narration phase the GM writes the reply the player reads, narrating the world-visible results of those changes, and only that text streams in. A status line shows the current phase (for example "Managing state...", "Running social_roll...", or "Narrating..."). A turn that waits for a player roll pauses between phases, so decisions can continue after the answer. A reply is saved only after the model finishes. Your message is recorded when you send it. If the reply fails, your message stays and the reply stays open: Continue retries it, and Discard reply removes the reply and the message that started it so you can send it again. While a reply is open you cannot send another message, and a pending roll must be answered first.

## Event log, log lines, and rollback

A campaign's state is stored only as events, in the `event` table, ordered by `seq`. There are no state tables: the locations, characters, notes, chat, and the open reply are computed by folding the log (`load_state` in `services/events.py`), and the GM's tools read and write through `WorldTools`. Each event has a channel. `Channel.LOG` events (`location.added`, `location.updated`, `location.removed`, `pc.added`, `npc.added`, `npc.updated`, `npc.removed`, `actor.moved`, `note.added`, `note.updated`, `note.removed`, `tool.ran`) appear in the chat as small log lines, between the messages in the order they happened. `Channel.TRANSCRIPT` events are the chat (`chat.player`, `chat.gm`). `Channel.INTERNAL` events (`turn.begun`, `turn.state_step`, `turn.prompted`, `turn.failed`, `speech.written`, `summary.updated`) drive the open reply and the summary and are not shown.

To add a kind of event, subclass `GameEvent` in `services/events.py` as a frozen dataclass with a unique `kind`, a `channel` (`Channel.LOG` by default), the fields it needs, `apply` (how it changes the folded state), and `render` (its Log text, or `None`), then `record` it from the command that makes the change. Payloads store resolved values, so replaying the log gives the same state.

Each AI reply has a rollback button. It deletes every event after that reply, so the state folds again from what is left, and any open reply is removed with them. Rollback cannot be undone.

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

- `rounds` lists the tool calls the GM makes, one list per round. The engine runs them for real, so each name must be a tool in the rule set. State steps get these rounds; the narration does not. A `social_roll` call needs a `prompt` argument, and it pauses the turn until the roll (or a text reply) is posted.
- `text` is the narration the player reads.
- `speech` maps an NPC's name to the dialogue its speech step returns. An NPC not listed there gets the intent back as its dialogue.
- `error` makes the reply fail. Your message is kept and the reply stays open, so Continue hits the same entry again; discard the reply to move on.
- A newer directive replaces the older one. When the script runs out, the model echoes your last message.

## Operations

```sh
uv run python -m cygnusforge.cli reset-password you@example.com
uv run pytest
uv run ruff check
```

Rate limiting is in memory and per process, so run a single worker.
