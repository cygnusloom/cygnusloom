# Cygnus Forge

A web chat over an LLM, growing into an RPG. Accounts and roles are owned by an external OpenID Connect provider; campaigns are named chats with a world template, a rule set, and a model. The model can be changed later from the campaign's settings, and the next message uses the new model. Campaigns also have structured role-play: locations, characters (the player character and NPCs), and notes.

## Run with Docker

The repository ships a compose stack with Cygnus Forge, Postgres, Authentik, and Caddy. It does
not include or provide a language model:

```sh
# Writes .env with generated secrets and your domain names
python3 scripts/setup_env.py --domain forge.example.com --idp-domain idp.example.com --email you@example.com
docker compose up -d --build
```

Without Python on the host, copy `.env.example` to `.env` and fill in the values manually.

Authentik creates the initial `akadmin` account from `AUTHENTIK_BOOTSTRAP_EMAIL` and
`AUTHENTIK_BOOTSTRAP_PASSWORD`. Sign in at `https://<AUTHENTIK_DOMAIN>/`, add `akadmin` to the
`cygnusforge-admins` group, then open `https://<CYGNUSFORGE_DOMAIN>/login` and sign in with the
same local account. Open **Admin → Models** and add at least one model; Cygnus Forge ships with
none. See [deploy/authentik/README.md](deploy/authentik/README.md) for local user setup and
troubleshooting.

### Local test server (no DNS)

```sh
python3 scripts/setup_env.py --domain localhost --idp-domain idp.localhost
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Open http://localhost:8000. Authentik is at http://idp.localhost:9000; sign in as `akadmin`
with the password in `.env`, then add `akadmin` to `cygnusforge-admins` as described in
[deploy/authentik/README.md](deploy/authentik/README.md). After signing in to Cygnus Forge, open
**Admin → Models** and add a model. This path uses plain HTTP and is for a local machine or a
trusted network only.

## Run locally

```sh
uv sync
# Put the CYGNUSFORGE_* settings from Configuration in .env (gitignored)
uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env --reload
```

The dev container (`.devcontainer/`) uses the uv image and reads `.devcontainer/devcontainer.env` as its env file. That file is gitignored, so keep your own copy of it. Both VS Code launch configurations load that file and set `CYGNUSFORGE_TEST_LOGIN` and `CYGNUSFORGE_TEST_LOGIN_ADMIN`, so the development login button appears.

For local development without an identity provider, set `CYGNUSFORGE_TEST_LOGIN=1` and use the
development login button. Open http://127.0.0.1:8000 and create a campaign. To change its model,
use the Settings button beside Send; the campaign header shows the current model.
`CYGNUSFORGE_SECRET_KEY` is required. No model is bundled: add one in **Admin → Models**, or seed
the catalog from the environment, for example `CYGNUSFORGE_LLM_PROVIDERS=fake/scripted` and
`CYGNUSFORGE_LLM_DEFAULT_MODEL=fake/scripted`. A local Ollama can be added as provider `ollama`,
model `gemma4:e2b`, base URL `http://localhost:11434/v1`.

## Accounts, single sign-on, and administration

Cygnus Forge is an OIDC relying party. It has no registration form, password store, password
reset, or MFA setup. Sign-in redirects to the configured provider; the account is created or
refreshed on the first successful sign-in. A user's profile page shows the identity and active
sessions, with a link to manage the account at the provider.

The bundled deployment uses Authentik local accounts: username/password accounts created in
Authentik, with no Google or Microsoft dependency. Additional users are created under
**Directory → Users** in Authentik; only users in `cygnusforge-admins` get the Cygnus Forge
administrator role.

Roles come from the provider claim named by `CYGNUSFORGE_OIDC_ADMIN_CLAIM` (default `groups`)
and are compared with `CYGNUSFORGE_OIDC_ADMIN_VALUES` (default `cygnusforge-admins`).
Cygnus Forge never edits roles. Removing a user from the group takes effect at their next
sign-in. The bundled Authentik blueprint creates the group and the OIDC client.

Sessions are stored server-side, so an administrator can revoke them and "sign out everywhere"
works immediately. A disabled account cannot sign in, and its existing sessions are revoked.
Destructive administrator actions require a sign-in inside `CYGNUSFORGE_STEP_UP_MINUTES`.

Administrators see `/admin`: account and campaign search, metadata-only campaign lookup, account
disable/enable, session revocation, campaign rename/model change/transfer/delete, model
configuration, and an append-only audit log. Campaign transcripts are not exposed to
administrators.

## Language models

Cygnus Forge does not provide a model. An administrator adds models in **Admin → Models**: pick a
provider, model name, optional base URL, and optional API key. The model id is
`provider/model`, such as `ollama/gemma4:e2b` or `openai/gpt-4o-mini`. One enabled model is the
default for new campaigns.

API keys are encrypted in the database with `CYGNUSFORGE_SECRET_KEY`; the admin page only shows
whether a key is configured. Environment variables such as `CYGNUSFORGE_LLM_PROVIDERS` and
`CYGNUSFORGE_LLM_<NAME>_BASE_URL` are a development seed: they are copied into the database on
first start if it has no models, and are not used to overwrite administrator changes.

## Upgrade and backups

Back up both databases before changing versions:

```sh
mkdir -p backups
docker compose exec -T db pg_dump -U cygnusforge cygnusforge > backups/cygnusforge-$(date +%F).sql
docker compose exec -T authentik-db pg_dump -U authentik authentik > backups/authentik-$(date +%F).sql
```

Normal application upgrade:

```sh
git pull
docker compose pull
docker compose up -d --build
```

The app container runs `alembic upgrade head` before starting Uvicorn, so database changes apply
automatically. After changing `.env`, recreate the affected service with `docker compose up -d app`;
`docker compose restart` does not reload environment values.

For an Authentik upgrade, change `AUTHENTIK_IMAGE` in `.env`, back up, then run
`docker compose pull authentik-server authentik-worker` and `docker compose up -d`. Upgrade one
major version at a time, as Authentik requires.

Never run `docker compose down -v`: it deletes the database and Authentik volumes. If you use the
local override, put `-f compose.yaml -f compose.local.yaml` before the compose subcommand.

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
  {"type": "world", "id": "twin-lakes", "name": "Twin Lakes", "description": "..."}
]
```

The types the engine understands today:

| Type | Where | Fields |
| --- | --- | --- |
| `world` | `worlds/<world>/` | `name` and `description`. Exactly one per world; `id` must equal the directory name. A world holds no locations or NPCs by default; the AI GM creates them. |
| `location` | `worlds/<world>/` | `name`, `description`. A place in the world. Optional `discovered` (true or false, default false): whether the player already knows the place. Optional `x` and `y`: where it lies on the world map, in kilometers east and north of the map's center (give both, or neither). Optional `parent`: the `id` of another location in the world that this one is inside, such as a building in a village. A place inside another is one level deep, has no `x` or `y`, and is not pinned on the map. No two places share a point unless the GM passes `force`. Locations and characters share one namespace, so no two may have the same name (ignoring case). |
| `npc` | `worlds/<world>/` | `name`, `location` (a location id), `description`. Optional `personality`, `accent`, and `quirks` (a list). Any trait left out is picked at random from the core tables. |
| `region` | `worlds/<world>/` | `name`, `terrain` (the id of a `world_terrain` kind in the rule set), and `points`: the corners of the outline in order around its edge, each `[x, y]` in kilometers from the map's center. An area of ground drawn on the world map. The regions of one world must not overlap, and the map shows a region once the player knows of it or a discovered place lies inside it. |
| `world_terrain` | `rulesets/<rule_set>/` | `name`, `color` (a hex color), and `rule` (what the ground is like for travel). Optional `pattern` (`waves`, `trees`, `ridges`, `reeds`, or `tufts`) and `ink` (the pattern's hex color). A kind of ground a world's regions may use. |
| `table` | `rulesets/<rule_set>/` | `options`: a list of strings to pick from. The `core` rule set must declare the tables `personality`, `accent`, and `quirk`; any rule set may replace them. |
| `creative_table` | `rulesets/<rule_set>/`, `worlds/<world>/` | `entries`: a non-empty list of unique strings, one per roll, rolled as `1dN` by the `roll_creative` tool when the GM wants an idea. `name` (optional, defaults to the id), `source` (optional, a credit for where the entries came from). The `core` rule set defines `action`, `theme`, `plot_twist`, `location`, and `location_descriptor`, from the Ironsworn Rulebook. A rule set may change a core table of the same id with `add` (entries appended) and `remove` (entries deleted; each must exist), or replace it with `entries`. A rule set table with `add` or `remove` but no core table of that id is an error. A patched table keeps the core `source` unless it sets its own. |
| `stat` | `rulesets/<rule_set>/` | `name`, and either `formula` (dice such as `3d6`) or `values` (a fixed list of whole numbers). Optional `swap_group`: stats in one group may swap one pair of rolled values at creation. A stat with `values` needs a `swap_group`; the stats in that group share the pool and may take its values in any order. |
| `feature` | `rulesets/<rule_set>/` | `name`, `feature_type` (`item`, `trait`, `spell`, or `feat`). Optional `description` and `bulky` (true or false). |
| `template` | `rulesets/<rule_set>/` | `kind` (a group such as `class` or `background`; the player picks one per kind), `name`, `description`. Optional `features` (a list of `{"feature": id, "quantity": "1" or a dice formula}`), `tables` (table ids rolled for the character), `names` (the id of a table of suggested character names; the player's name dice draws from the chosen templates' names, or every template's when none has any; these are not choices), `quote`, `stats` (an object of stat id to bonus, added when the template is chosen), and `counters` (an object of counter id to amount, added to the counter's starting value). |
| `character_field` | `rulesets/<rule_set>/` | `name`, `description`. A text field when it has no `table`; otherwise a choice from that table. |
| `rule_set` | `rulesets/<rule_set>/` | `name`, `description`. Exactly one per rule set; `id` must equal the directory name. Optional `condition_ends`: the list of triggers, besides `gm`, that a condition's `ends` may name (Cairn 2e uses `rest`, `extended_rest`, `aid`, and `time`). |
| `rule` | `rulesets/<rule_set>/` | `text`: a plain-text rule the AI GM follows. Optional `important` (true or false, default false): important rules are given to the GM in full in every prompt. Every other rule is listed by name with a short brief, and its full text comes from `lookup_rule`. |
| `move` | `rulesets/<rule_set>/` | `label`, `description`, `dice` (a formula such as `2d6`), and either `stat` (a stat id) or `stat_choice` (a list of stat ids the player picks from when rolling). `bands`: a non-empty list of `{"label", "min"?, "text", "effects"?}`, checked in order. The band with no `min` is the fallthrough. `effects` use the same ops as conditions and injuries (`note`, `set_max`, `condition`, `save`, `pick_stat`, `counter`, `end_condition`, `retire`). Optional `needs_all_conditions` (true or false): the move is for a character who has every condition, as in an exit roll. |
| `counter` | `rulesets/<rule_set>/` | `label`, `description`; optional `min`, `max`, and `initial` (default `min`), and `full`: effects that run when the counter reaches its maximum. A counter a character lacks reads as its initial value. |
| `condition` | `rulesets/<rule_set>/` | `label`, `description`; `ends`: the triggers that end it (from `rule_set`'s `condition_ends`, plus `gm`); `on_end`: effects that run when it ends; `modifies`: an object of stat id to amount added to that stat, where `"*"` means every stat; optional `dying` and `fatal` (true or false). A condition with `derived` (`stat`, `current`, `needs_max`) is not stored: it holds while the stat's current score is exactly `current`, and, with `needs_max`, while the stat's maximum is above 0. |
| `python_module` | `rulesets/<rule_set>/` | `module`: a dotted import path, imported when the content loads. |
| `combat` | `rulesets/<rule_set>/` | `python_module` (optional): the id of a `python_module` entry that defines `save_succeeds(roll, score)` and `initiative_order(entries)`, which override the declarative rules below. `save` (`mode`: `roll_under` or `roll_over`; `auto_success` and `auto_fail`: rolls on the check die that always pass or fail, or null) and `initiative_mode` (`pass_npcs_fail` or `roll_order`) decide saves and round-one order. `hit_mode`: `auto` (a hit always lands) or `roll_vs_defense` (the check die must reach the target's armor). `multi_attack`: `highest` or `each`. `hit_points`, `overflow`, and `initiative_stat`: stat ids. Hit points absorb damage first, the overflow stat takes the rest, and the target makes a save on that stat when it runs out. |
| `monster` | `rulesets/<rule_set>/` | `name`, `description`; `stats`: a whole number for each stat in the rule set and no others; optional `armor` (default 0) and `attacks`, an object of attack name to dice formula such as `"1d8"` or `"1d6+1d6"`. A template the GM searches and copies when it creates an NPC. |
| `tool_call` | `rulesets/<rule_set>/` | `name`: the tool name the GM sees (letters, digits, `_`, `-`; unique in the rule set); `description`: when and how to use it, including each parameter; `parameters`: a JSON schema object (`type` `object`, with `properties` and optional `required`); `python_module`: the id of a `python_module` entry; `function`: the name of a callable in that module; `player_input` (optional): `{"dice": "1d20", "param": "roll"}` pauses the turn so the player rolls the dice, and passes the total to the function as `param`, or the player's free-form reply instead; `{"dice_param": "damage_dice", "param": "damage"}` does the same with the dice taken from the `damage_dice` argument, a formula such as `"1d8+1d6"` that the GM sets, so the GM chooses the dice and the player rolls them; `details` (optional): a template such as `"Modifiers: {relationship:+d}."` shown with the prompt, filled from the arguments and checked at load (it may only name schema properties); `activity` (optional): a template such as `"You rolled {die}."` shown to the player in the turn log, filled from the arguments and the result. |

A `tool_call`'s parameters must match its function. Every schema property must be a keyword argument of the function, and every argument without a default must be a required property. Load fails otherwise. A string parameter may say `"enum_from": "moves"` or `"enum_from": "stats"` to take its choices from the rule set's moves or stats, so they cannot drift from the content. A tool with `player_input` also needs a required string `prompt` property: the GM writes the question the player sees, such as "Roll to persuade him." Other types are read but not used yet. Within one directory, each `(type, id)` pair must be unique. Content loads at startup, so a bad file stops the app with an error that names the file.

Character options: a rule set describes its characters with `stat`, `feature`, `template`, and `character_field` entries. A template groups features and tables under a kind, and the player picks one template per kind (or leaves it to be rolled). Every part is optional, so an NPC can be given any combination; anything left out is rolled or left blank. The engine builds a JSON schema from these entries (`character_schema` in [content/character.py](src/cygnusforge/content/character.py)), and the GM's `create_npc` tool and the creation form both check their input against it. A rule set that defines no stats, templates, or features gets a schema with only the fields.

Content is trusted code. A `python_module` imports a Python module, which runs its top-level code. Only add rule sets you would run as Python.

Bundled content:

Every campaign uses the `core` rule set, whatever its rule set. It is merged under the selected one: a definition in a rule set replaces the core definition with the same type and id, and a rule set can add its own. The core rule set defines the role-play rules, the role-play tools, and the NPC trait tables.

- `core` rule set (loaded for every campaign): the role-play rules, the tools `create_location` (optional `force`, to put it on a point another place has, and optional `parent`, to put it inside a top-level location such as a city), `update_location` (optional `x` and `y` to move it, `off_map` to take it off the map, `force`), `discover_location`, `delete_location`, `create_region` (optional `force`, to overlap ground on purpose; otherwise the part over existing ground is cut away), `create_npc`, `update_npc`, `delete_npc`, `move_actor`, `add_note`, `update_note`, `delete_note`, `search_notes`, the encounter tools `create_encounter_map`, `update_encounter_map`, `delete_encounter_map`, `start_encounter`, `end_encounter`, `add_participant`, `remove_participant`, `place_participant`, and `end_turn`, the lookup tools `all_at_location`, `search_location`, `search_npc`, `get_statblock`, `character_options`, `lookup_rule`, and `list_lookups`, and `speak` (optional `force`, to speak an NPC who is away from the PC) and `ooc`, the tables `personality`, `accent`, and `quirk`, the creative tables `action`, `theme`, `plot_twist`, `location`, and `location_descriptor` (rolled by `roll_creative`, optional `tables` to name which ones), and the character fields `appearance` and `biography`, which every rule set has. It is not selectable on its own. The creative tables are from the Ironsworn Rulebook by Shawn Tomkin, licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/; https://ironswornrpg.com), taken from the Datasworn transcription by rsek (CC BY 4.0). The entries are the rulebook's text. The rulebook gives some `location` entries more weight than others; here each entry is equally likely. The rulebook's `region` table is left out, since its entries link to pages of the Ironlands atlas.
- `cairn2e` rule set: Cairn 2e (https://cairnrpg.com/second-edition/). Stats Strength, Dexterity, and Willpower are rolled 3d6 and may swap one pair within the group, and Hit Protection is 1d6. The 20 backgrounds are the templates. They grant items (petty or bulky), a Backpack, gold, and gear, and roll their own tables. The character fields (physique, skin, hair, face, speech, clothing, virtue, vice, and bond) are rolled from tables. The omen is a campaign option: it is chosen once when the campaign is created, before any character, and the GM sees it in its context. Its combat rules (`combat` `cairn`, in the `cairn2e` module) follow the Warden's guide: attacks always hit, armor reduces each hit, the rest goes to Hit Protection, then to Strength, and a character whose Strength is used up makes a Strength save against Critical Damage, while a hit to exactly 0 HP rolls on the Scars table. Eighty-four bestiary monsters are included. The GM searches them with `search_monster` and makes an NPC with `create_monster`, from a template or from its own values. The rule set's tools cover combat, saves, initiative, armor, injuries, rest, deprivation, inventory, magic, marketplace, hirelings, dungeon cycles, and wilderness actions. The rule set's `encounter_grid` sets the encounter cell size and movement: 5 ft cells and 40 ft of movement, so 8 cells per turn. The content is taken verbatim from cairnrpg.com by a generator script that asserts each description and table entry matches the source. The game was written by Yochai Gal and is licensed CC BY-SA 4.0.
- `twin-lakes` world: the region near Isthmus Town in Vald, from the CAS-1 module _Trouble in Twin Lakes_. It is the test world for Cairn 2e. Its overland locations and routes, the Temple of the Fungal Lizards, its NPCs, its features, its monsters, its regions, its rules, and its tables are imported from the CAS-1 module. Use it with the `cairn2e` rule set.

Tool calls: each message offers the rule set's `tool_call` entries to the model as function tools. When the model calls one, the engine runs the Python function with the model's arguments and returns the result to the model, which then writes its reply. A message may run any number of rounds of tool calls, and nothing caps them: a model that never stops calling tools keeps the reply open until you discard it. Bad calls (unknown name, invalid JSON, wrong or missing or unexpected arguments, or a function that raises) return an `error` object to the model instead of failing the turn, so it can recover. The transcript keeps the chat lines. The turn's tool activity (for example "You rolled 14 on the d20...") goes into the chat as a small log line, and the raw tool results are in the Transcript tab.

Player input: a tool call with `player_input` pauses the turn. The page shows the GM's prompt, its `details` line (such as the modifiers), and a Roll button (or a text box for a free-form reply). The server rolls the dice when you click, so the turn cannot be rerolled. The prompt is saved with the turn, so it survives a page reload or a restart, and the composer stays disabled until you answer. Your answer is saved before the reply continues, so a later failure never rolls the dice again. The dice use a random generator by default; tests seed it.

The Journal panel shows the campaign's journal: one text file that the GM alone writes and the player reads. The journal step runs at the opening and at each day end: a turn whose clock moves into a new campaign day, including a jump across several days, which counts as one. Other turns skip the step. When it runs, it covers every log line since the journal was last written, so it can span turns. It writes one passage per covered day: it appends a new day's passage, or merges into the current day's passage with `replace_in_journal` (the old text must match exactly once; empty new text deletes it). The journal is kept to one passage per campaign day, and the GM condenses older days when it runs long. The narration step can search it with `search_journal` and read line ranges with `view_journal`. The journal has no length cap.

Character creation: a new campaign opens on a full-page creation form with the sidebar (Journal, Characters) still available, and the chat stays closed until the player character exists. The form is built from the rule set's character options. The player's stats are rolled when the form opens (Reroll stats rolls them again), and a swappable stat can take any rolled value from its group. Each choice shows its chosen value on one line. The Choose button opens a dialog with the full options as radio cards (picking one closes it), scrolled to the chosen option if there is one, and the dice button beside it picks one at random. The toolbar above the form has "Randomize all", which rolls every visible choice (including ones already picked) and then the name, and "Randomize all not chosen", which fills only the empty choices and the name if it is blank. The name is rolled last so it draws on the chosen background's names. Text fields other than the name stay as the player left them. Named options (such as the Item and Specialty tables in the Cairn backgrounds) show their name as the card title with the description beneath; the sheet stores only the name. Every template and choice must be set before the form is accepted; the form points at the first missing one, and the text fields may stay blank. The engine resolves the rest at creation. The PC starts unplaced, and the GM places it with `move_actor` on its first turn. That turn is the campaign's opening, and the page starts it by itself once the character exists: the GM places the PC, makes any opening reading or roll the rule set calls for, and introduces itself, each important detail the player must know, and each player character, one short sentence apiece, as setup rather than play, then hands over to the player. The composer stays disabled until the opening finishes, and the player's first message comes after it. The opening is not shown as a player message, and it cannot be discarded.

Structured role-play: every campaign has a player character (the PC), created as described above. The PC starts unplaced, and the GM places it with `move_actor`. Once placed, the PC is an actor like an NPC, and every placed actor is at exactly one location. The world's locations and NPCs are created with the campaign. The GM manages the world with tools: it can create, update, and delete locations (`create_location`, `update_location`, `delete_location`) and NPCs (`create_npc`, `update_npc`, `delete_npc`), move an actor (`move_actor`), and write, update, and delete notes (`add_note`, `update_note`, `delete_note`, by note id). A location with someone in it cannot be deleted until they move, and the player character cannot be updated as an NPC or deleted. Notes are the GM's own record of events, updates, and situations, so it stays consistent; they are not written for the player, and the narration never reveals them. A note is campaign-wide, or attached to a location or an actor; deleting a location or NPC keeps the notes about it. The GM finds notes with `search_notes`, which matches every word of the query against the note's text and subject, newest first, and returns each note's id. The world map shows the discovered locations that have a position, with the player character's place marked, or the nearest place that holds it when they are inside a building. A location is discovered when the player character goes there, or when the GM calls `discover_location` because the player has heard of it in detail; the GM can also create a location as discovered. Ground on the map is made of regions: a world may author them, and the GM adds the rest with `create_region` when the story leaves the authored map. The GM is asked for six or more uneven corners rather than a rectangle, and the ground already there is cut away, so a new region fills the open ground beside the old ones. A region is revealed once the player knows of it or a discovered place lies inside it, and a place with a map point must sit inside a region with 0.5 km of ground around it. The map is a square centered on the player character's place that grows to fit the farthest discovered place or known region corner, and the campaign page shows it in the right panel. Neither map has a legend: each place or participant is a short token (its initials, or its first three letters for a one-word name, lengthened where two tokens on the same map would match), and hovering over it shows the full name in a popup styled like the token. The map panel takes 40% of the right panel's height, or 60% during an encounter, and during an encounter a turn-order list sits beneath the encounter map, scrolling within itself if it runs long. The campaign page shows the characters, locations, and notes live, but only what the player can reach: the Location panel shows the PC's location with its description and the PC and NPCs there, the Notes panel shows campaign-wide notes and notes about the PC, its location, and the NPCs there, and the Character panel shows the PC's details and sheet. The activity log is a history of what the GM wrote, so it still names places and NPCs elsewhere. Names must be unique across locations and characters, and a character name cannot match an NPC already in the campaign.

Encounters: an encounter is a fight or other timed scene on an encounter map. A map belongs to a location and is a grid of cells (up to 60 by 60), with a name and description. The GM creates, edits, and deletes maps (`create_encounter_map`, `update_encounter_map`, `delete_encounter_map`; a map in use cannot be deleted). `update_encounter_map` can rename a map (`new_name`, which must be unused), move it to another location (`location`), and change its size and description; a map in use follows its encounter. `start_encounter` opens an encounter on a map, and only one runs at a time. The GM adds and removes participants with `add_participant` and `remove_participant`, and `place_participant` puts one on a cell (0-based, from the top left). Adding someone who is not at the map's location gives a warning, not an error. Each participant is on a side, a free-form name given as `side` to `add_participant`: player characters default to `party` and NPCs to `foes`. When a Cairn encounter starts, each player character rolls DEX for initiative with `player_initiative` (a save) before anyone acts, and no turn begins until everyone has rolled. Passing characters act first, then NPCs, then failing characters. From round two the party acts together, then each other side in the order it first joined. Movement is limited by the rule set's `encounter_grid`, and diagonal steps count as one cell. The first placement is free, and a participant's movement resets when its turn ends. A player's turn starts by itself as soon as it is next. The player sends a message to act, then presses End your turn. On an NPC's turn, the player presses Start NPC's turn, and the GM plays it out and calls `end_turn`; if the GM forgets, the turn closes when its reply finishes. The player can type something else instead of starting an NPC's turn. The turn buttons sit above the chat input. The GM can call `end_encounter` and `remove_participant` at any time; removing the participant whose turn it is passes the turn on. An encounter ends on its own when one side can no longer fight: when the last foe falls, or when the player characters are all knocked out. A defeated participant stays on the map as a body, faded and struck through, with status `dead` for an NPC or `in critical damage` for a player character, and the roster shows the status beside the name. A knocked-out player character is unconscious in Cairn's critical damage; the GM brings them round with `recover_character`, which takes a narrative explanation and consequence, and their harm stays. While an encounter runs, the right panel's Map section opens on its encounter map by default, with each participant's token on its cell. The world map is the other tab of the same section. Both maps, here and in the Map view, can be panned by dragging and zoomed with the mouse wheel (double-click resets). Zoom is not kept across a page reload.

Limited context: the GM's prompt holds only the campaign options, the PC and its location, and the NPCs at that location. The GM looks up everything else with its lookup tools, and is told to use them liberally: `all_at_location` describes a place and the NPCs there, `search_location` and `search_npc` find places and NPCs by any words in their details, and `get_statblock` returns the sheet of the PC or an NPC. Statblocks stay out of the prompt until a tool asks for them. `lookup_rule` returns the full text of a rule, and `list_lookups` returns a short brief of every lookup tool, rule, location, and NPC. The prompt lists every rule with a brief, and gives the important rules in full. The speech step reuses the same system prompt, so it also sees the lookup instruction, but it has no tools.

NPC traits (personality, accent, and quirks) are picked at random from the core tables when the NPC does not have them. They are not part of the GM's prompt; they reach the model only through speech.

Speech: the GM does not write dialogue in its narration. To have an NPC speak, it calls `speak` with the NPC's name and the intent. The engine then makes a separate speech step with the same model as the turn. That step gets the NPC's details (who they are, personality, accent, quirks, and location) and the intent, and returns the dialogue. The dialogue reaches the narration as JSON, `{"queued_dialogue": [{"speaker": ..., "dialogue": ...}]}`, in the order it was said, and the narration copies each `dialogue` value exactly. Each line of dialogue is saved as soon as it is written, so it survives a failed narration; the narration is saved when the reply finishes. An NPC should be at the PC's location to speak. If it is not, `speak` writes nothing and returns a warning, and the GM can call it again with `force` true when the NPC is speaking from elsewhere, such as across a wall; the narration then says where they are speaking from.

Settings are fixed when a campaign is created. There is no backwards compatibility at version 0:
databases from earlier versions need a new one. Run `python -m cygnusforge.cli migrate` for a new
database, or delete `cygnusforge.db` and let the development app create it.

For a throwaway database, run the "Cygnus Forge: web server (temp database)" launch configuration in VS Code. It runs `scripts/temp_server.py`, which creates a new SQLite database in a temp directory on each start and deletes it when the server stops. It does not reload on code changes, so restart it after editing code.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CYGNUSFORGE_SECRET_KEY` | (required) | Signs the session cookie and the transient OIDC state |
| `CYGNUSFORGE_ENVIRONMENT` | `development` | `development` or `production`. Production requires OIDC settings, HTTPS, and test login off |
| `CYGNUSFORGE_BASE_URL` | `http://127.0.0.1:8000` | Public origin; the OIDC redirect URI is `<base_url>/auth/callback` |
| `CYGNUSFORGE_DATABASE_URL` | `sqlite:///./cygnusforge.db` | SQLAlchemy URL; Postgres uses `postgresql+psycopg://...` |
| `CYGNUSFORGE_OIDC_ISSUER` | none | Provider issuer URL, for example `https://idp.example.com/application/o/cygnusforge/` |
| `CYGNUSFORGE_OIDC_CLIENT_ID` | none | OIDC client id |
| `CYGNUSFORGE_OIDC_CLIENT_SECRET` | none | OIDC client secret |
| `CYGNUSFORGE_OIDC_SCOPES` | `openid email profile groups` | Requested scopes; `openid` is always added |
| `CYGNUSFORGE_OIDC_NAME` | `Single sign-on` | Login button label and provider name |
| `CYGNUSFORGE_OIDC_ADMIN_CLAIM` | `groups` | Claim that holds roles; dotted paths such as `realm_access.roles` work |
| `CYGNUSFORGE_OIDC_ADMIN_VALUES` | `cygnusforge-admins` | Comma-separated role values that grant administrator access |
| `CYGNUSFORGE_OIDC_ACCOUNT_URL` | none | Optional link to the provider's profile page |
| `CYGNUSFORGE_SESSION_HOURS` | `168` | Server-side session lifetime |
| `CYGNUSFORGE_STEP_UP_MINUTES` | `15` | How recently an administrator must have signed in for destructive actions; `0` disables the check |
| `CYGNUSFORGE_SECURE_COOKIES` | follows `BASE_URL` | Set to `1` behind HTTPS; uses the `__Host-` cookie prefix |
| `CYGNUSFORGE_AUTO_CREATE_SCHEMA` | on in development, off in production | Create missing tables at startup. The Docker entrypoint runs Alembic instead |
| `CYGNUSFORGE_LLM_PROVIDERS` | none | Development seed for the model catalog; after first start, use Admin → Models |
| `CYGNUSFORGE_LLM_DEFAULT_MODEL` | none | Development seed for the default model; the first listed model is used when omitted |
| `CYGNUSFORGE_LLM_CONTEXT_TOKENS` | `32768` | Context window used to estimate prompt size. At 50% the older chat is summarized, keeping the newest messages worth about 10% of the window verbatim |
| `CYGNUSFORGE_LLM_<NAME>_BASE_URL` | `ollama` has a default | OpenAI-compatible base URL for provider `<name>` |
| `CYGNUSFORGE_LLM_<NAME>_API_KEY` | none | API key for provider `<name>` |
| `CYGNUSFORGE_LOG_LEVEL` | `INFO` | Log level |
| `CYGNUSFORGE_TEST_LOGIN` | off | Development-only button that signs in a test account by email. It is refused in production |
| `CYGNUSFORGE_TEST_LOGIN_ADMIN` | off | Development-only: the test account gets the administrator role. Requires `TEST_LOGIN` |

Token counts are estimates (about four characters per token). The campaign page shows the estimated prompt size next to the settings button. The full transcript is always kept; only the prompt sent to the model is summarized. A summary is asked to stay within about 25% of the window, and if it still runs longer, its oldest text is dropped.

A reply has two phases. In the state management phase the GM decides what the action changes and what needs to be said or done, and makes the tool calls that change the world, notes, and NPC speech; its text is not shown. If the player is speaking out of character (asking the GM about the rules, the setup, or the game itself), the state phase calls `ooc` instead, with no other tool calls. The out-of-character phase then answers the question in its first sentence, in a few sentences, with all tools available. Its text streams to the player and is saved as a GM message. The `ooc` argument `no_narration` decides what follows: when true the reply is the whole turn, and when false the story continues in a second message with the narration phase. Text streamed before a tool call in this phase stays visible even though the reply goes on, so the model is told to make its tool calls before writing text. In the narration phase the GM writes the reply the player reads, narrating the world-visible results of those changes, and only that text streams in. The narration is kept minimal: a routine beat takes a sentence or two, and more is written only when something really happened. A status line shows the current phase (for example "Managing state...", "Running lookup_rule...", or "Narrating..."). A turn that waits for a player roll pauses between phases, so decisions can continue after the answer. A reply is saved only after the model finishes. Your message is recorded when you send it. If the reply fails, your message stays and the reply stays open: Continue retries it, and Discard reply removes the reply and the message that started it so you can send it again. While a reply is open you cannot send another message, and a pending roll must be answered first.

## Event log, log lines, and rollback

A campaign's state is stored only as events, in the `event` table, ordered by `seq`. There are no state tables: the locations, characters, notes, chat, and the open reply are computed by folding the log (`load_state` in `services/events.py`), and the GM's tools read and write through `WorldTools`. Each event has a channel. `Channel.LOG` events (`location.added`, `location.updated`, `location.removed`, `location.discovered`, `pc.added`, `npc.added`, `npc.updated`, `npc.removed`, `actor.moved`, `note.added`, `note.updated`, `note.removed`, `tool.ran`) appear in the chat as small log lines, between the messages in the order they happened. `Channel.TRANSCRIPT` events are the chat (`chat.player`, `chat.gm`). `Channel.INTERNAL` events (`turn.begun`, `turn.state_step`, `turn.prompted`, `turn.failed`, `speech.written`, `summary.updated`) drive the open reply and the summary and are not shown.

To add a kind of event, subclass `GameEvent` in `services/events.py` as a frozen dataclass with a unique `kind`, a `channel` (`Channel.LOG` by default), the fields it needs, `apply` (how it changes the folded state), and `render` (its Log text, or `None`), then `record` it from the command that makes the change. Payloads store resolved values, so replaying the log gives the same state.

Each AI reply has a rollback button. It deletes every event after that reply, so the state folds again from what is left, and any open reply is removed with them. Rollback cannot be undone.

Each AI reply has a gear button. It opens a "Reply details" dialog with two tabs. The Context tab shows exactly what the model received for that reply (the system prompt, rules, tool definitions, the summary, the kept messages, and any tool rounds), with each part labelled by its source. The Transcript tab shows everything that happened in that round: your message, tool calls and their results, and the reply. Each reply stores its own copy of that context and transcript, so long chats use more database space. Replies from before these features show "No context was recorded" or no transcript.

Test-only content lives in [tests/fixtures/](tests/fixtures/): the `rolltest` rule set (a `social_roll` tool that asks the player for a d20 roll, backed by [tests/rolls.py](tests/rolls.py)) and the `testhollow` world. Tests merge it into the app's content; the app does not show it.

Model ids take the form `provider/model`, for example `ollama/gemma4:e2b` or `openai/gpt-4o-mini`. Configure them in **Admin → Models**; the `CYGNUSFORGE_LLM_*` variables only seed the catalog on first start in development.

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
uv run python -m cygnusforge.cli migrate
uv run python -m cygnusforge.cli list-users
uv run pytest -n auto
uv run ruff check
```

`migrate` applies Alembic migrations. The Docker entrypoint runs it before starting Uvicorn.
`list-users` prints accounts read-only, for operators who cannot reach the admin panel.

```sh
docker compose logs -f app
docker compose exec db pg_dump -U cygnusforge cygnusforge > cygnusforge.sql
docker compose pull && docker compose up -d --build
```

Back up both Postgres databases and the Authentik media volume before an upgrade. The app has no
uploads yet; campaign content lives entirely in the app database.

`-n auto` runs the tests on all CPUs with pytest-xdist and takes under a minute. A plain
`uv run pytest` is refused at startup. For a serial run with readable output or a debugger, set
`CYGNUSFORGE_SERIAL_TESTS=1` (`--pdb` does not work with `-n`).

Rate limiting is in memory and per process, so run a single app worker, or move the limiter to
the proxy before scaling out.
