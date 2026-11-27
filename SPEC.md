Cygnus Loom is an RPG engine that uses an AI game master to run the game, with a comprehensive engine to ensure the AI GM plays the game correctly, follows the rules, and keeps track of everything.

# Campaigns, Rulesets, and Worlds
A campaign is a running game instance. Its event log is the source of truth; state is built from it, and it supports rollback and editing the latest GM reply. Each campaign has a ruleset, which determines how the AI GM can act, how things behave, when dice are used, and so on. A campaign is based on a world, the template for the setting. It may start from a module, which can supply that world, a starting situation, or a short side story. A campaign can carry a private uploaded adventure source (PDF, Markdown, or text) the GM can search and read.

Rulesets, worlds, and modules are JSON. Shipped rulesets: Core (engine base only), Worlds Without Number, Cairn 2e, Academia Arcana, SRD 5.2.1 (5E compatible), and Hordes. Shipped worlds: Unwritten World, Unwritten Academy, and The Unwritten Apocalypse. Shipped modules: Trouble in Twin Lakes and The Hermit of Ash Hollow. A new campaign defaults to Worlds Without Number and Unwritten World. Campaigns export as compressed JSON and import between servers; older versions are converted on import.

# Accounts and Sharing
On a server, players sign in through OIDC (Authentik); the desktop edition uses a local account. A campaign is private to its owner until shared through a join link. Invited players become members, and each makes their own player character. The owner can remove and re-add members. Players see each other by a chosen display name; emails stay private. Authentik groups separate admins, credit-exempt accounts, and players. Admin actions need a step-up login; sessions can be revoked.

Every player's page follows the campaign live. Any player can message the GM, but only one reply runs at a time. A prompt belongs to the character it names, and only that character's player answers it. Any player can start an NPC's turn; the owner can end another player's turn.

# Creating a Campaign
The New Campaign form chooses a ruleset, a world or module, an optional AI model, and the campaign options. The options cover the experience track, inventory, survival, hireling turns, starting level (and whether it follows the owner's level), respec permission, creative genres and tones, and any ruleset settings. It also accepts the adventure source. The owner can change these and the model later in Settings; the newest options reach the GM on the next reply.

Creation starts a party: the player character and any starting hirelings. The opening turn is recorded only when the player starts the game. Later players build their characters when they join. The owner can reset the campaign to before the opening, refunding credits for removed replies, or delete it.

# Characters
Character creation comes from the ruleset's JSON: templates, stats, backgrounds, species, classes, skills, features, items, and starting picks. Players can roll stats, use a standard array, randomize, or start from a template. At a raised starting level, the character chooses starting picks on the creation form; later levels use the advancement page. A waiting level blocks play until spent.

A sheet tracks stats, skills, features, items, armor, currency, conditions, and location. A player edits their own character's flavor; the GM owns hireling flavor. The owner can allow respecs. A respec keeps name, pronouns, description, flavor, gear, and story place, and refunds stats, templates, skills, features, levels, and picks.

# Player Companions and Hirelings
The player can obtain hirelings or party companions; the engine treats both as hirelings. They may be free (joined of their own volition, supplied at game start) or cost a wage, and have their own personalities and drives while following player directions. A hireling has an employer, wage, sheet, inventory, and levels where the ruleset has them. A starting hireling serves at wage zero. A hireling can be dismissed, or promoted to player character if the player character dies. The player controls hirelings in combat, or the GM can play their turns and roll their dice.

# Player Dice
Whenever the player needs to roll, the game stops and asks, like a real GM. The prompt names the character and asks for a check, save, attack, reaction, choice, or text. The player rolls or answers in text; the engine applies modifiers and advantage or disadvantage, rolls damage, and records the breakdown in the log. NPCs and GM-played hirelings are rolled by the engine. A secret check shows the player's dice and outcome but hides the cause and difficulty; a secret GM roll stays out of the default transcript.

# Encounters
Encounters slow time so actions resolve by the rules, usually for combat. Turn order comes from the ruleset's initiative: side initiative keeps each side together; individual initiative keeps each character's order. The order is fixed for the encounter. A player starts an NPC's turn and the GM runs it; the player's own turn starts automatically and they end it. A GM-played hireling's turn also waits for Start.

The engine tracks the acting character's attacks, movement, and reactions. Attacks use melee reach, ranged bands, and line of sight; an out-of-range or blocked attack is refused. Hazards, traps, morale, conditions, death saves, and concentration are engine-resolved where supported. The GM may adjust the order only for a held action, a side's order, surprise, or a correction. Not every ruleset uses encounters.

# AI Game Master - Cygnus
Cygnus is the AI GM. It keeps the game on track, follows the rules, and allows no inconsistency. The story belongs to the players, so Cygnus changes the story or scene if they insist.

A turn runs in stages: a state/tool step, narration, a review that can retract it, a journal, and speech. Speech writes NPC dialogue. The GM changes state only through tools; the engine refuses illegal calls and says what to do instead. A force option covers story-only exceptions. The review retracts narration that contradicts the log.

The opening has its own prompt and retry; out-of-character replies do not advance the game. A checklist reminds the GM to cover characters, cast, notes, storylines, time, rules, encounters, maps, market, and the rest. The GM can roll secretly, prompt a player, and roll for hirelings it plays. Context is summarized as it grows; a turn keeps running if the player navigates away, and the page catches up live. The GM uses an OpenAI-compatible, streamed model; a server offers a catalog, and the desktop setup takes the player's own model.

# World, Time, and Travel
The world is places, regions, roads, peoples, and factions. The GM uses tools to create and update them, along with NPCs, monsters, and encounters. A world can come from the ruleset's setting tables or be written by hand. The world map shows known places, roads, and threats; the local map shows the grid, terrain, objects, and tokens. Maps use the ruleset's terrain and objects, with board, plan, or blueprint styles.

Time moves in parts, hours, or days. Travel can be overland, a journey, wilderness, a dungeon, or a city route, depending on the ruleset. Rests, food and water, survival, and named progress clocks follow the ruleset. Places can be searched for loot, danger, and clues; sites, shelter, and projects are tracked where the ruleset uses them.

# Story and Memory
The event log is the record. The GM keeps notes, NPC knowledge, storylines, a journal, and speech. A storyline records the live question, its forces, and its pressure; the GM keeps them current and searches them before inventing. Summaries compress old turns. A player can edit the latest GM reply and roll the campaign back.

Modules and creative tables give the GM starting material and new places, people, and complications. The GM rolls them before inventing when it needs a new direction.

# Other Campaign Systems
Inventory and currency are tracked per character; a campaign can also track inventory space. Items are content, and the market is built from priced items. Crafting, vehicles, services, factions and their assets, projects, shelter improvements, sites, campaign clocks, and threats are generic systems the ruleset can use. Magic is a ruleset system: spells, slots, casting, and spell attacks. Harm, conditions, injuries, death, recovery, counters, and timers are engine systems the ruleset can use.

# Music
Cygnus sets a scene mood. The browser chooses a bundled track for it, crossfades it, and remembers the player's mute and volume. Music starts on the first click, tap, or key press, goes quiet in the background, and returns when the player does. Combat and a party with no character left in play set moods automatically. Music is bundled, so it works offline and never calls a third-party service.

# Generic Engine, JSON rules
The engine must support various rulesets. It should not be a hodgepodge of spaghetti code as new rulesets pile on. Ruleset behavior should be defined in the ruleset JSON, and the engine itself must remain clean. Use modular systems with hooks, parameters, and customizable behavior so each ruleset can define how the engine behaves. If custom code is absolutely necessary, it can live in each ruleset's Python module. Most RPG concepts are similar enough to live in the generic core engine and be parameterized in the JSON ruleset.

A ruleset is a bundle of JSON definitions; the core supplies the defaults, and a definition can add to, override, or replace a core one. Content is found by its identifying entry, not by file or folder name, and every bundle is validated at load. Stable ids and explicit order keep content stable across files.

# Credits and Costs
On a server, the server owner provides the models. A saved GM reply costs credits; a source document costs either a number of replies or an amount per 100,000 characters, set per model. The campaign owner pays; admins, credit-exempt accounts, and single-player do not. Admins grant credits, with no self-service checkout, and usage and charges go in a ledger.

# Interfaces and Operations
A campaign workspace has Play, Map, Journal, Characters, Compendium, and Settings. Play shows the scene, character, location, factions, projects, inventory, clocks, and shelter. The compendium shows the content in chapters and works on its own. Admins have pages for users, campaigns, models, pricing, credits, and audit. The workspace works on phones and desktops.

The server runs in Docker with Postgres, Authentik, and nginx. `scripts/install.sh` sets it up; `backup.sh` and `update.sh` back it up and upgrade it, and migrations run on start. The desktop app bundles the server and local database, has a setup wizard, and can connect to remote servers or accept invites. Releases are built for Windows and Linux. The app is AGPL-3.0-or-later; third-party content keeps its own license and is credited on `/license`.

# Unit tests
The game has many unit tests. These must be regularly optimized and evaluated, as they can take a significant amount of time to run. They should also be evaluated to ensure that useless unit tests are removed or never written. Tests use a fake or scripted model, never a live one. The suite covers unit, integration, and distribution tests via `scripts/test.py`; live UI checks use a real browser. CI runs `ruff check .`.

# Updating the SPEC

The spec must be kept brief. Use the same voice for new changes. Prefer re-writing to clarify rather than just adding new text.
