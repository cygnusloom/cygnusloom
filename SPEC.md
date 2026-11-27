Cygnus Loom is an RPG engine that uses an AI game master to run the game, with a comprehensive engine to ensure the AI GM plays the game correctly, follows the rules, and keeps track of everything.

# Campaigns, Rulesets, and Worlds
A campaign is a running game instance. It consists of an event log tracking everything that happens - this is the source of truth. State is folded from the log, and the log supports rollback and editing the latest GM reply. Each campaign has a ruleset, which determines how the AI GM can act, how things behave, when dice are used, and so on. A campaign is also based on a world, which is the template for setting up the game world for the player and AI GM to play in. It may also start from a module, which can supply that world, a starting situation, or a short side story. A campaign can carry an uploaded adventure source (PDF, Markdown, or plain text) that the GM can search and read. The source is private to the campaign, and is limited to a 1 GB file and 100 million characters of extracted text.

Rulesets, worlds, and modules are JSON content. The shipped rulesets are Core (the engine base, not a playable game), Worlds Without Number, Cairn 2e, Academia Arcana, SRD 5.2.1 (5E compatible), and Hordes. The shipped worlds are Unwritten World, Unwritten Academy, and The Unwritten Apocalypse. The shipped modules are Trouble in Twin Lakes and The Hermit of Ash Hollow. A new campaign defaults to Worlds Without Number and Unwritten World. Campaigns can be exported and imported between servers; exports are compressed JSON, and older campaign versions are converted on import.

# Accounts and Sharing
On a server, players sign in through OIDC (Authentik); the desktop edition uses a local account. A campaign is private to its owner until the owner shares it. The owner invites players with a join link, and invited players become members. Each player makes their own player character. Players see each other by a display name they choose; account email addresses stay private. Authentik groups decide the role: cygnusloom-admins administer the server, accounts in cygnusloom-credit-exempt do not spend credits on campaigns they own, and cygnusloom-players are ordinary players. Admin actions require a step-up login, and sessions can be revoked.

Every player's page follows the campaign live. Any player can message the GM, but only one GM reply runs at a time. When the GM asks a player, the prompt belongs to the character it names, and only that character's player answers it. Any player can start an NPC's turn, and the owner can end another player's turn.

# Creating a Campaign
The New Campaign form chooses a ruleset, a world or module, an optional AI model, and the campaign options. The options include the experience track, inventory space, survival needs, hireling turns, the starting level (and whether it follows the owner's level), respec permission, creative genres and tones, and any setting details the ruleset defines. The form also accepts an optional adventure source. The owner can change the options and the model later in Settings; the newest options reach the GM on the next reply.

Creation starts a starting party: the player character and any starting hirelings. The opening turn is recorded only when the player starts the game. Players who join later build their characters when they join. The owner can reset a campaign to just before the opening, which returns the credits charged for removed replies, or delete it.

# Characters
Character creation comes from the ruleset's JSON: templates, stats, backgrounds, species, classes, skills, features, items, and starting picks. Players can roll stats, use a standard array, randomize, or start from a character template. A character at a raised starting level chooses its starting picks on the creation form; later levels use the advancement page. A waiting level blocks play until it is spent.

A sheet tracks stats, skills, features, items, armor, currency, conditions, and the character's place in the story. A player edits their own character's flavor; the GM owns hireling flavor. The owner can allow respecs. A respec keeps the character's name, pronouns, description, flavor, gear, and place in the story, and refunds stats, templates, skills, features, levels, and picks.

# Player Companions and Hirelings
The player can obtain hirelings or party companions during play; the engine treats both as hirelings. They may be free (joined of their own volition, supplied at game start) or cost a wage. They have their own personalities and drives but follow player directions. A hireling is a character with an employer, a wage, a sheet, inventory, and levels where the ruleset has them. A starting hireling serves at wage zero. A hireling can be dismissed, and if the player character dies, a living hireling can be promoted to player character. The player controls hirelings in combat, or the GM can play their turns and roll their dice.

# Player Dice
Whenever the player needs to roll some dice, the game stops and asks, like a real GM. The prompt names the character and asks for a roll, a choice, or text. For a roll, the player rolls the dice or answers in text; the engine adds the modifiers, rolls damage, and records the full breakdown in the log. NPCs and GM-played hirelings are rolled by the engine. A secret check shows the player's own dice and outcome but hides the cause and difficulty, and a secret GM roll stays out of the default transcript.

# Encounters
Encounters are scenes where simulated time slows down so actions can be resolved by the rules, usually for combat. Turn order comes from the ruleset's initiative: side initiative keeps each side together, and individual initiative keeps each character's order. The order is fixed for the encounter. A player starts an NPC's turn and the GM runs it; the player's own turn starts automatically and they end it. A GM-played hireling's turn also waits for Start.

The engine tracks the acting character's attacks, movement, and reactions. Attacks use melee reach, ranged bands, and line of sight; an attack out of range or through a blocker is refused. Hazards, traps, morale, conditions, death saves, and concentration are resolved by the engine where the ruleset supports them. The GM may adjust the order only for a held action, a side choosing its order, surprise, or a correction. Not every ruleset uses encounters.

# AI Game Master - Cygnus
Cygnus is the AI GM. It keeps the game on track, follows all the rules, and does not allow any inconsistency. However, the story is for the players, so if they insist, Cygnus will change the story or scene to better fit what story they want to tell.

A turn runs in stages: a state step where the GM calls tools, narration, a review that can retract narration, a journal, and speech. Speech writes what NPCs say. The GM changes state only through tools. The engine checks every tool call and refuses anything illegal, telling the GM what to do instead. A force option exists for story-only exceptions. The review retracts narration that contradicts the log.

The opening has its own prompt and retry; out-of-character replies do not advance the game. A turn checklist reminds the GM to cover characters, cast, notes, storylines, time, rules, encounters, maps, market, and the rest. The GM can roll dice secretly, prompt a player, and roll for hirelings it plays. Context is summarized as it grows. A turn keeps running if the player navigates away, and the page catches up live. The GM uses an OpenAI-compatible chat model, streamed; a server offers a catalog of models, and the desktop setup wizard lets the player enter their own.

# World, Time, and Travel
The world is a set of places, regions, roads, peoples, and factions. The GM creates and updates them with tools. A world can be generated from the ruleset's setting tables or written by hand. The world map shows known places, roads, and threats; the local map shows the encounter grid, terrain, objects, and tokens. Maps use the ruleset's terrain and objects, and can use a board, plan, or blueprint style.

Time moves in parts, hours, or days. Travel can be overland, a journey, wilderness, a dungeon, or a city route, depending on the ruleset. Rests, food and water, survival, and named progress clocks follow the ruleset. Places can be searched for loot, danger, and clues; sites, shelter, and projects are tracked where the ruleset uses them.

# Story and Memory
The event log is the record. The GM keeps notes, situations called storylines, a journal, and speech. A storyline records the live question, the forces in play, and the pressure on it; the GM keeps them current and searches them before inventing. Summaries compress old turns. A player can edit the latest GM reply and roll the campaign back.

Modules and creative tables give the GM starting material and new places, people, and complications. The GM rolls the tables before inventing when it needs a new direction.

# Other Campaign Systems
Inventory and currency are tracked per character; a campaign can also track inventory space. Items are content, and a market is built from items with prices. Crafting, vehicles, services, factions, projects, shelter improvements, sites, campaign clocks, and threats are generic systems the ruleset can use. Harm, conditions, death, recovery, counters, and timers are engine systems the ruleset can use.

# Music
Cygnus sets a mood for the scene. The browser chooses a bundled track for that mood, crossfades it, and remembers the player's mute and volume. Music starts on the first click, tap, or key press, goes quiet in the background, and comes back when the player returns. Combat and a party with no character left in play set moods automatically. Music is bundled, so it works offline and never calls a third-party service.

# Generic Engine, JSON rules
The engine must support various rulesets. It should not be a hodgepodge of spaghetti code as new rulesets pile on. Instead, ruleset behavior should be defined in the ruleset JSON, and the engine itself must remain clean. Use modular systems with hooks, parameters, and customizable behavior so each ruleset can define how the engine behaves. If custom code is absolutely necessary, it can be contained within each ruleset's Python module. However, most RPG concepts are similar enough that they can live within the generic core engine and just be parameterized in the JSON ruleset.

A ruleset is a bundle of JSON definitions, and the core supplies the defaults. A definition can add to, override, or replace a core one. Content is found by its identifying entry, not by file or folder name, and every bundle is validated at load. Stable ids and explicit order keep content stable across files.

# Credits and Costs
On a server, the server owner provides the models. A saved GM reply costs credits, and a source document has its own price, either a number of replies or an amount per 100,000 characters, set per model. The campaign owner pays. Admins, accounts in cygnusloom-credit-exempt, and single-player do not pay. Credits are granted by an admin; there is no self-service checkout. Usage and charges are recorded in a ledger.

# Interfaces and Operations
A campaign workspace has Play, Map, Journal, Characters, Compendium, and Settings. Play shows the scene, character, location, factions, projects, inventory, clocks, and shelter. The compendium shows the campaign's content in chapters, and also works on its own. Admins have pages for users, campaigns, models, pricing, credits, and audit. The same workspace works on phones and desktops.

The server runs in Docker with Postgres, Authentik, and nginx. `scripts/install.sh` sets it up, and `backup.sh` and `update.sh` back up and upgrade it; migrations run on start. The desktop app bundles the server and a local database, has a setup wizard, and can also connect to remote servers or accept invites. Releases are built for Windows and Linux. The app is AGPL-3.0-or-later, and third-party content keeps its own license and is credited on `/license`.

# Unit tests
The game has many unit tests. These must be regularly optimized and evaluated, as they can take a significant amount of time to run. They should also be evaluated to ensure that useless unit tests are removed or never written. Tests use a fake or scripted model and never call a live model. The suite covers unit, integration, and distribution tests and runs through `scripts/test.py`; live UI checks use a real browser. CI runs `ruff check .`.

# Updating the SPEC

The spec must be kept brief. Use the same voice for new changes. Prefer re-writing to clarify rather than just adding new text.
