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

Source distributions include only the project paths listed in `pyproject.toml`. Both source
distributions and wheels exclude `.env`, `.env.*` (except `.env.example`), `*.env`, `*.log`, and
common SQLite databases and sidecars. Untracked files within included directories can still be
packaged; update the allowlist when adding release files and inspect archives before publishing.

## Ruleset design

Rulesets are collections of JSON definitions in `src/cygnusforge/data/rulesets`. They inherit
the shared `core` definitions and can replace a definition by type and ID. Game systems are
enabled by their definitions; ruleset-specific Python is an escape hatch, not a requirement for
new rules. Definitions and their references are checked when the content library loads.

Character templates can restrict combinations without naming a particular RPG concept:

- `compatible_with` maps another template kind to the template IDs allowed alongside it.
  When a chosen template names a kind, that kind must have a selection from the allowed IDs.
  The form hides a kind with no compatible option and skips it, so full classes can share a
  ruleset with partial classes that require a second `class2` and a second partial class that
  requires a `tradition` (`no_tradition` or a partial tradition). A missing compatible choice is
  refused with the template's name and the kind it needs.
- `template_requirements` lists kinds that must be selected when the template is chosen.
  Set `distinct` to require different choices. Templates in different kinds can use the same
  `choice_key` when they represent the same choice. Optional `missing_message` and
  `duplicate_message` provide ruleset-specific explanations.
- A ruleset with skills defines `skill_progression` on its `rule_set` definition, with integer
  `untrained`, `first_level`, `creation_maximum`, and `maximum` values. Training is tracked
  separately from its level, so a trained level can equal the untrained value. A skill pick's
  optional `max_level` limits both chosen and randomly selected skills, and its optional `label`
  identifies the choice on the creation form. A move's optional
  `skill_floor` defaults to zero; use `null` to apply negative skill levels without a floor.
  A template's `skills` map contains positive grant counts: one grant trains a skill at
  `first_level`, and each additional grant raises it by one, up to the creation maximum.
- A derived stat may name single-skill picks in `plus_skill_picks`. The chosen or rolled skill is
  recorded on the character and its current level contributes while its template is selected.
  Referenced picks must choose exactly one skill and cannot collide with character fields.
- To replace excess creation skill grants instead of discarding them, configure
  `skill_progression.creation_replacements` with optional `roll_exclude` and `feature_exclude`
  skill-ID lists. Players can enter ordered `skill_replacements.roll` and
  `skill_replacements.feature` choices; an empty or omitted choice rolls an eligible skill.
  Excess rolled or template grants use the first list, and feature effects use the second.
  An invalid choice or an exhausted eligible pool is an error. Creation effects run once for
  each granted copy of a feature.
- A template's tagged `choose` requires its full `count` of distinct features. If a tag has too
  few options, creation reports an error. Add field values to `skip_tags` when the ruleset
  explicitly requires no picks for those values.
- A `feat_picks` entry also fills its complete `count`: blank slots are rolled from its available
  features, and an insufficient pool is an error. Overlapping random pools are allocated together
  so that one pick does not needlessly prevent another.
- Set a feature pick's `advance_repeated_rank` to let a repeated ranked feature advance to the
  unique feature with the same name, type, and tag at the next rank. A ruleset can list
  `ordered_feature_tags` to require lower ranks and prevent repeated maximum ranks. A feature
  marked `repeatable` can instead be taken again as a distinct choice; for a feature with a next
  rank, `feature_repeats` selects that alternative for the corresponding pick. Advancement
  preserves repeatable feature counts and enforces the same ordered-rank prerequisites.
  Character descriptions show ordered ranks and repeat counts separately.

Hooks may name a `configure` callable in their ruleset module to validate and normalize their
`options` at load time. The callable receives the options, character specification, and definition
label, and returns a mapping. Without a configurator, options are passed through unchanged.
For ruleset-specific algorithms that need structured data, a `rule_data` definition names a
`python_module` and `configure` callable. The callable validates the definition against the
character specification and returns a value available to tools through `ctx.world.rule_data`.
These values belong to the loaded ruleset, rather than global module state.

Combat can name an `unarmed.skill` and a `level_damage` object with `template_kind`, `templates`,
`counter`, and `divisor`. Their references are checked at load time; neither mechanic depends on
particular class, skill, or counter names. An `injuries` object specifies its `table`, `stat`,
`trigger` (`depleted` or `damage`), and the affected `target_kinds` (`pc` and/or `npc`).
The optional `healing_stat` names the stat healed when a tool call does not specify one; without
it, healing calls must name a stat.

When combat defines a nonzero `defense_base`, defense uses the configured `defense_modifier` and
explicitly equipped body armor and shield. Starting item features are carried and the best armor
and shield are equipped; stowing, using up, or dropping equipment removes it from its slot.
A feature can define `defense_floor` with a nonnegative `base` and, optionally, a `counter`,
positive `divisor`, and `round_up` (default `true`). `requires_no_armor` and `requires_no_shield`
can limit a floor to empty equipment slots. The highest applicable floor or worn armor applies
before the modifier and shield. Defense recalculates as equipment, relevant stats, or counters change.
Setting a tracked total records an adjustment that persists across those changes. An untracked
statblock keeps its printed total; explicitly equipping it turns that total into an innate baseline.

The optional `dungeon` rule data uses `cygnusforge.services.dungeon.configure_dungeon` to configure
an `event_table`, a `reaction_table`, or both. Its `triggers` list and result-to-text `notes`
mapping apply to event rolls. Table IDs can refer to ruleset or world tables and are resolved when
rolled.

Magic item rules name distinct `reusable`, `single_use`, and `charged` kinds. Casting results and
events use those configured kind names.

Inventory rules may name `pools`, each with an `id`, a `name`, and either a fixed `capacity` or a
capacity derived from a `stat`: the raw score times `multiplier`, plus `base`, divided by
`divisor`, and rounded `up` or `down`. At most one pool sets `"ready": true` for the items a
character can use without spending an action. `size_pools` maps an item size id to the pool its
items occupy, and `default_pool` names the pool for every other size; a ruleset that names
`pools` must name a `default_pool`, and both references are checked at load time. A ruleset with
no `pools` keeps the single-pool model: `capacity` is the fixed size,
or `capacity_stat` names the stat whose raw score, times `capacity_multiplier`, sets it, and the
optional `readied_stat`/`readied_divisor` give the to-hand slots.

In the 2.0 ruleset format, replace a template's old `classes` list with
`"compatible_with": {"class": [...]}`, define `skill_progression` for any ruleset with skills,
and configure the injury trigger and targets explicitly. Configure `healing_stat` if healing calls
may omit the stat. The loader rejects the old template field rather than silently ignoring a
restriction.

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
