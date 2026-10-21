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
  {"type": "world", "id": "kilharbor", "name": "Kilharbor", "description": "..."}
]
```

The types the engine understands today:

| Type | Where | Fields |
| --- | --- | --- |
| `world` | `worlds/<world>/` | `name` and `description`. Exactly one per world; `id` must equal the directory name. A world holds no locations or NPCs by default; the AI GM creates them. |
| `location` | `worlds/<world>/` | `name`, `description`. A place in the world. Optional `discovered` (true or false, default false): whether the player already knows the place. Optional `x` and `y`: where it lies on the world map, in kilometers east and north of the map's center (give both, or neither). Optional `parent`: the `id` of another location in the world that this one is inside, such as a building in a village. A place inside another is one level deep, has no `x` or `y`, and is not pinned on the map. No two places share a point unless the GM passes `force`. Locations and characters share one namespace, so no two may have the same name (ignoring case). |
| `npc` | `worlds/<world>/` | `name`, `location` (a location id), `description`. Optional `personality`, `accent`, and `quirks` (a list). Any trait left out is picked at random from the core tables. |
| `table` | `rulesets/<rule_set>/` | `options`: a list of strings to pick from. The `core` rule set must declare the tables `personality`, `accent`, and `quirk`; any rule set may replace them. |
| `stat` | `rulesets/<rule_set>/` | `name`, `formula` (dice such as `3d6`). Optional `swap_group`: stats in one group may swap one pair of rolled values at creation. |
| `feature` | `rulesets/<rule_set>/` | `name`, `feature_type` (`item`, `trait`, `spell`, or `feat`). Optional `description` and `bulky` (true or false). |
| `template` | `rulesets/<rule_set>/` | `kind` (a group such as `class` or `background`; the player picks one per kind), `name`, `description`. Optional `features` (a list of `{"feature": id, "quantity": "1" or a dice formula}`), `tables` (table ids rolled for the character), `names` (the id of a table of suggested character names; the player's name dice draws from the chosen templates' names, or every template's when none has any; these are not choices), and `quote`. |
| `character_field` | `rulesets/<rule_set>/` | `name`, `description`. A text field when it has no `table`; otherwise a choice from that table. |
| `rule_set` | `rulesets/<rule_set>/` | `name`, `description`. Exactly one per rule set; `id` must equal the directory name. |
| `rule` | `rulesets/<rule_set>/` | `text`: a plain-text rule the AI GM follows. Optional `important` (true or false, default false): important rules are given to the GM in full in every prompt. Every other rule is listed by name with a short brief, and its full text comes from `lookup_rule`. |
| `python_module` | `rulesets/<rule_set>/` | `module`: a dotted import path, imported when the content loads. |
| `tool_call` | `rulesets/<rule_set>/` | `name`: the tool name the GM sees (letters, digits, `_`, `-`; unique in the rule set); `description`: when and how to use it, including each parameter; `parameters`: a JSON schema object (`type` `object`, with `properties` and optional `required`); `python_module`: the id of a `python_module` entry; `function`: the name of a callable in that module; `player_input` (optional): `{"dice": "1d20", "param": "roll"}` pauses the turn so the player rolls the dice, and passes the total to the function as `param`, or the player's free-form reply instead; `details` (optional): a template such as `"Modifiers: {relationship:+d}."` shown with the prompt, filled from the arguments and checked at load (it may only name schema properties); `activity` (optional): a template such as `"You rolled {die}."` shown to the player in the turn log, filled from the arguments and the result. |

A `tool_call`'s parameters must match its function. Every schema property must be a keyword argument of the function, and every argument without a default must be a required property. Load fails otherwise. A tool with `player_input` also needs a required string `prompt` property: the GM writes the question the player sees, such as "Roll to persuade him." Other types are read but not used yet. Within one directory, each `(type, id)` pair must be unique. Content loads at startup, so a bad file stops the app with an error that names the file.

Character options: a rule set describes its characters with `stat`, `feature`, `template`, and `character_field` entries. A template groups features and tables under a kind, and the player picks one template per kind (or leaves it to be rolled). Every part is optional, so an NPC can be given any combination; anything left out is rolled or left blank. The engine builds a JSON schema from these entries (`character_schema` in [content/character.py](src/cygnusforge/content/character.py)), and the GM's `create_npc` tool and the creation form both check their input against it. A rule set that defines no stats, templates, or features gets a schema with only the fields.

Content is trusted code. A `python_module` imports a Python module, which runs its top-level code. Only add rule sets you would run as Python.

Bundled content:

Every campaign uses the `core` rule set, whatever its rule set. It is merged under the selected one: a definition in a rule set replaces the core definition with the same type and id, and a rule set can add its own. The core rule set defines the role-play rules, the role-play tools, and the NPC trait tables.

- `core` rule set (loaded for every campaign): the role-play rules, the tools `create_location` (optional `force`, to put it on a point another place has, and optional `parent`, to put it inside a top-level location such as a city), `update_location` (optional `x` and `y` to move it, `off_map` to take it off the map, `force`), `discover_location`, `delete_location`, `create_npc`, `update_npc`, `delete_npc`, `move_actor`, `add_note`, `update_note`, `delete_note`, `search_notes`, the encounter tools `create_encounter_map`, `update_encounter_map`, `delete_encounter_map`, `start_encounter`, `end_encounter`, `add_participant`, `remove_participant`, `place_participant`, and `end_turn`, the lookup tools `all_at_location`, `search_location`, `search_npc`, `get_statblock`, `character_options`, `lookup_rule`, and `list_lookups`, and `speak` and `ooc`, the tables `personality`, `accent`, and `quirk`, and the character fields `appearance` and `biography`, which every rule set has. It is not selectable on its own.
- `cairn2e` rule set: Cairn 2e (https://cairnrpg.com/second-edition/). Stats Strength, Dexterity, and Willpower are rolled 3d6 and may swap one pair within the group, and Hit Protection is 1d6. The 20 backgrounds are the templates. They grant items (petty or bulky), a Backpack, gold, and gear, and roll their own tables. The character fields (physique, skin, hair, face, speech, clothing, virtue, vice, and bond) are rolled from tables. The omen is a campaign option: it is chosen once when the campaign is created, before any character, and the GM sees it in its context. Backgrounds are structure only: they have no mechanical effects yet. The rule set's `encounter_grid` sets the encounter cell size and movement: 5 ft cells and 40 ft of movement, so 8 cells per turn. The content is taken verbatim from cairnrpg.com by a generator script that asserts each description and table entry matches the source. The game was written by Yochai Gal and is licensed CC BY-SA 4.0.
- `kilharbor` world: a cold, ancient forest harbor, where the PCs arrive by ship to seek treasure and knowledge. It has no pre-generated locations or NPCs; the AI GM creates everything. Use it with the `cairn2e` rule set.

Tool calls: each message offers the rule set's `tool_call` entries to the model as function tools. When the model calls one, the engine runs the Python function with the model's arguments and returns the result to the model, which then writes its reply. A message may run any number of rounds of tool calls, and nothing caps them: a model that never stops calling tools keeps the reply open until you discard it. Bad calls (unknown name, invalid JSON, wrong or missing or unexpected arguments, or a function that raises) return an `error` object to the model instead of failing the turn, so it can recover. The transcript keeps the chat lines. The turn's tool activity (for example "You rolled 14 on the d20...") goes into the chat as a small log line, and the raw tool results are in the Transcript tab.

Player input: a tool call with `player_input` pauses the turn. The page shows the GM's prompt, its `details` line (such as the modifiers), and a Roll button (or a text box for a free-form reply). The server rolls the dice when you click, so the turn cannot be rerolled. The prompt is saved with the turn, so it survives a page reload or a restart, and the composer stays disabled until you answer. Your answer is saved before the reply continues, so a later failure never rolls the dice again. The dice use a random generator by default; tests seed it.

Character creation: a new campaign opens on a full-page creation form with the sidebar (Lore, Notes, Characters) still available, and the chat stays closed until the player character exists. The form is built from the rule set's character options. The player's stats are rolled when the form opens (Reroll stats rolls them again), and a swappable stat can take any rolled value from its group. Each choice shows its chosen value on one line. The Choose button opens a dialog with the full options as radio cards (picking one closes it), scrolled to the chosen option if there is one, and the dice button beside it picks one at random. The toolbar above the form has "Randomize all", which rolls every visible choice (including ones already picked) and then the name, and "Randomize all not chosen", which fills only the empty choices and the name if it is blank. The name is rolled last so it draws on the chosen background's names. Text fields other than the name stay as the player left them. Named options (such as the Item and Specialty tables in the Cairn backgrounds) show their name as the card title with the description beneath; the sheet stores only the name. Every template and choice must be set before the form is accepted; the form points at the first missing one, and the text fields may stay blank. The engine resolves the rest at creation. The PC starts unplaced, and the GM places it with `move_actor` on its first turn. That turn is the campaign's opening, and the page starts it by itself once the character exists: the GM places the PC, makes any opening reading or roll the rule set calls for, and introduces itself, each important detail the player must know, and each player character, one short sentence apiece, as setup rather than play, then hands over to the player. The composer stays disabled until the opening finishes, and the player's first message comes after it. The opening is not shown as a player message, and it cannot be discarded.

Structured role-play: every campaign has a player character (the PC), created as described above. The PC starts unplaced, and the GM places it with `move_actor`. Once placed, the PC is an actor like an NPC, and every placed actor is at exactly one location. The world's locations and NPCs are created with the campaign. The GM manages the world with tools: it can create, update, and delete locations (`create_location`, `update_location`, `delete_location`) and NPCs (`create_npc`, `update_npc`, `delete_npc`), move an actor (`move_actor`), and write, update, and delete notes (`add_note`, `update_note`, `delete_note`, by note id). A location with someone in it cannot be deleted until they move, and the player character cannot be updated as an NPC or deleted. Notes are the GM's own record of events, updates, and situations, so it stays consistent; they are not written for the player, and the narration never reveals them. A note is campaign-wide, or attached to a location or an actor; deleting a location or NPC keeps the notes about it. The GM finds notes with `search_notes`, which matches every word of the query against the note's text and subject, newest first, and returns each note's id. The world map shows the discovered locations that have a position, with the player character's place marked. A location is discovered when the player character goes there, or when the GM calls `discover_location` because the player has heard of it in detail; the GM can also create a location as discovered. The map is a square centered on the origin that grows to fit the farthest discovered place, and the campaign page shows it in the right panel. Neither map has a legend: each place or participant is a short token (its initials, or its first three letters for a one-word name), and hovering over it shows the full name. The campaign page shows the characters, locations, and notes live, but only what the player can reach: the Characters panel shows the PC and the NPCs at its location, the Notes panel shows campaign-wide notes and notes about the PC, its location, and the NPCs there, and the Sheet panel shows the PC's sheet. The activity log is a history of what the GM wrote, so it still names places and NPCs elsewhere. Names must be unique across locations and characters, and a character name cannot match an NPC already in the campaign.

Encounters: an encounter is a fight or other timed scene on an encounter map. A map belongs to a location and is a grid of cells (up to 60 by 60), with a name and description. The GM creates, edits, and deletes maps (`create_encounter_map`, `update_encounter_map`, `delete_encounter_map`; a map in use cannot be deleted). `update_encounter_map` can rename a map (`new_name`, which must be unused), move it to another location (`location`), and change its size and description; a map in use follows its encounter. `start_encounter` opens an encounter on a map, and only one runs at a time. The GM adds and removes participants with `add_participant` and `remove_participant`, and `place_participant` puts one on a cell (0-based, from the top left). Adding someone who is not at the map's location gives a warning, not an error. Turn order is the order participants joined, for now. Movement is limited by the rule set's `encounter_grid`, and diagonal steps count as one cell. The first placement is free, and a participant's movement resets when its turn ends. A player's turn starts by itself as soon as it is next. The player sends a message to act, then presses End your turn. On an NPC's turn, the player presses Start NPC's turn, and the GM plays it out and calls `end_turn`; if the GM forgets, the turn closes when its reply finishes. The player can type something else instead of starting an NPC's turn. The turn buttons sit above the chat input. `end_encounter` and `remove_participant` are refused while someone is taking a turn. While an encounter runs, the right panel's Map section opens on its encounter map by default, with each participant's token on its cell and a line listing anyone not on the map. The world map is the other tab of the same section.

Limited context: the GM's prompt holds only the campaign options, the PC and its location, and the NPCs at that location. The GM looks up everything else with its lookup tools, and is told to use them liberally: `all_at_location` describes a place and the NPCs there, `search_location` and `search_npc` find places and NPCs by any words in their details, and `get_statblock` returns the sheet of the PC or an NPC. Statblocks stay out of the prompt until a tool asks for them. `lookup_rule` returns the full text of a rule, and `list_lookups` returns a short brief of every lookup tool, rule, location, and NPC. The prompt lists every rule with a brief, and gives the important rules in full. The speech step reuses the same system prompt, so it also sees the lookup instruction, but it has no tools.

NPC traits (personality, accent, and quirks) are picked at random from the core tables when the NPC does not have them. They are not part of the GM's prompt; they reach the model only through speech.

Speech: the GM does not write dialogue in its narration. To have an NPC speak, it calls `speak` with the NPC's name and the intent. The engine then makes a separate speech step with the same model as the turn. That step gets the NPC's details (who they are, personality, accent, quirks, and location) and the intent, and returns the dialogue. The dialogue reaches the narration as JSON, `{"queued_dialogue": [{"speaker": ..., "dialogue": ...}]}`, in the order it was said, and the narration copies each `dialogue` value exactly. Each line of dialogue is saved as soon as it is written, so it survives a failed narration; the narration is saved when the reply finishes. An NPC can only speak when it is at the PC's location; otherwise the tool returns an error and the GM can recover.

Settings are fixed when a campaign is created. The schema changed, so databases from earlier versions need a new one: delete `cygnusforge.db` or point `CYGNUSFORGE_DATABASE_URL` somewhere new.

For a throwaway database, run the "Cygnus Forge: web server (temp database)" launch configuration in VS Code. It runs `scripts/temp_server.py`, which creates a new SQLite database in a temp directory on each start and deletes it when the server stops. It does not reload on code changes, so restart it after editing code.

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

A reply has two phases. In the state management phase the GM decides what the action changes and what needs to be said or done, and makes the tool calls that change the world, notes, and NPC speech; its text is not shown. If the player is speaking out of character (asking the GM about the rules, the setup, or the game itself), the state phase calls `ooc` instead, with no other tool calls. The out-of-character phase then replies directly, with all tools available. Its text streams to the player and is saved as a GM message. The `ooc` argument `no_narration` decides what follows: when true the reply is the whole turn, and when false the story continues in a second message with the narration phase. Text streamed before a tool call in this phase stays visible even though the reply goes on, so the model is told to make its tool calls before writing text. In the narration phase the GM writes the reply the player reads, narrating the world-visible results of those changes, and only that text streams in. A status line shows the current phase (for example "Managing state...", "Running lookup_rule...", or "Narrating..."). A turn that waits for a player roll pauses between phases, so decisions can continue after the answer. A reply is saved only after the model finishes. Your message is recorded when you send it. If the reply fails, your message stays and the reply stays open: Continue retries it, and Discard reply removes the reply and the message that started it so you can send it again. While a reply is open you cannot send another message, and a pending roll must be answered first.

## Event log, log lines, and rollback

A campaign's state is stored only as events, in the `event` table, ordered by `seq`. There are no state tables: the locations, characters, notes, chat, and the open reply are computed by folding the log (`load_state` in `services/events.py`), and the GM's tools read and write through `WorldTools`. Each event has a channel. `Channel.LOG` events (`location.added`, `location.updated`, `location.removed`, `location.discovered`, `pc.added`, `npc.added`, `npc.updated`, `npc.removed`, `actor.moved`, `note.added`, `note.updated`, `note.removed`, `tool.ran`) appear in the chat as small log lines, between the messages in the order they happened. `Channel.TRANSCRIPT` events are the chat (`chat.player`, `chat.gm`). `Channel.INTERNAL` events (`turn.begun`, `turn.state_step`, `turn.prompted`, `turn.failed`, `speech.written`, `summary.updated`) drive the open reply and the summary and are not shown.

To add a kind of event, subclass `GameEvent` in `services/events.py` as a frozen dataclass with a unique `kind`, a `channel` (`Channel.LOG` by default), the fields it needs, `apply` (how it changes the folded state), and `render` (its Log text, or `None`), then `record` it from the command that makes the change. Payloads store resolved values, so replaying the log gives the same state.

Each AI reply has a rollback button. It deletes every event after that reply, so the state folds again from what is left, and any open reply is removed with them. Rollback cannot be undone.

Each AI reply has a gear button. It opens a "Reply details" dialog with two tabs. The Context tab shows exactly what the model received for that reply (the system prompt, rules, tool definitions, the summary, the kept messages, and any tool rounds), with each part labelled by its source. The Transcript tab shows everything that happened in that round: your message, tool calls and their results, and the reply. Each reply stores its own copy of that context and transcript, so long chats use more database space. Replies from before these features show "No context was recorded" or no transcript.

Test-only content lives in [tests/fixtures/](tests/fixtures/): the `rolltest` rule set (a `social_roll` tool that asks the player for a d20 roll, backed by [tests/rolls.py](tests/rolls.py)) and the `testhollow` world. Tests merge it into the app's content; the app does not show it.

Model ids take the form `provider/model`, for example `ollama/gemma4:e2b` or `openai/gpt-4o-mini` with `CYGNUSFORGE_LLM_OPENAI_BASE_URL` set.

## Scripted fake model (for tests)

`fake/scripted` reads a directive from your message: `__FAKE__:` followed by a strict JSON object (quoted keys). Each user message after the directive uses the next entry in `turns`:

```text
__FAKE__:{"turns": [
  {"text": "Hello."},
  {"rounds": [[{"name": "lookup_rule", "arguments": {"rule": "locations"}}]],
   "text": "The rule is clear."},
  {"error": "upstream exploded"}
]}
```

- `rounds` lists the tool calls the GM makes, one list per round. The engine runs them for real, so each name must be a tool in the rule set. State steps get these rounds; the narration does not. A tool whose `player_input` asks for a roll pauses the turn until the roll (or a text reply) is posted. The test-only `rolltest` rule set has one, `social_roll`.
- `text` is the narration the player reads.
- `ooc` is the out-of-character reply, used when a state step calls `ooc` and the out-of-character phase has no tool calls left. Without it that reply is empty and fails.
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
