Cygnus Forge is an RPG engine that uses an AI game master to run the game, with a comprehensive engine to ensure the AI GM plays the game correctly, follows the rules, and keeps track of everything.

# Campaigns, Rulesets, and Worlds
A campaign is a running game instance. It consists of an event log tracking everything that happens - this is the source of truth. Each campaign has a ruleset, which determins how the AI GM can act, how things behave, when dice are used, and so on. A campaign is also based on a world, which is the template for setting up the game world for the player and AI GM to play in.

# AI Game Master - Cygnus
Cygnus is the AI GM. Cygnus will keep the game on track, following all the rules and not allowing any inconsistency. However, Cygnus understands that the story is for the players, so if the players insist, Cygnus will change the story or scene to better fit what story the players want to tell.

# Player Dice
Whenever the player needs to roll some dice, the game will stop and let the player choose to roll their dice (prompted accordingly) or enter a free-form response. This is equivalent to a real game master asking their player to roll the dice, and the player can either roll the dice or respond to the GM in another way.

# Encounters
Encounters are scenes within the game where simulated time slows down so actions can be resolved according to the ruleset, usually for combat. Actors in encounters will often take turns. The player clicks on a button to start each actors turn. The AI GM will run NPC turns. For their own turn, the player also clicks on a button to end it.

# Generic Engine, JSON rules
The engine should support various rulesets. It should not be a hodgepodge of spaghetti code as new rulesets pile on. Instead, ruleset behavior should be defined in the ruleset JSON. The engine itself must remain clean. Use modular systems with hooks, parameters, and customizable behavior so each ruleset can define how the engine behaves. If custom code is absolutely necessary, it can be contained within each ruleset's python module. However, most RPG concepts are similar enough that they can live within the generic core engine and just be parameterized in the JSON ruleset.