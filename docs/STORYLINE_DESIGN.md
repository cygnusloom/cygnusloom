# Storyline design for the AI game master

This document is the design behind Cygnus Loom's storytelling guidance. It explains what the AI
GM is trying to do, why the old "storyline as plot" model produced flat games, and how the engine
and prompts now support stories that grow out of play. The GM-facing version of this doctrine is
implemented in the core rule set's `storylines` rule, the `story_craft` and `encounter_craft`
prompt fragments, and the `create_storyline` / `update_storyline` tool schemas.

## The problem

The recorded symptoms were generic, safe prose; samey NPCs and plots; and weak pacing and
consequences. They share one cause: the GM was writing a plot instead of running a situation.

A tabletop session is not a story being told. It is a situation being played. The moment the GM
prepares a sequence of events, every player choice becomes either an interruption or a detour back
to the prepared path. The same failure shows up in an AI GM: it writes a tidy plot summary, then
steers every scene toward it, or it waits passively for the player because nothing in the world is
moving on its own.

The old storyline record encouraged exactly that. It stored a player-facing name and summary plus a
private "plot", but asked for no dramatic question, no forces with goals, and no pressure. In live
play the GM used it as a summary of what had already happened rather than a live situation, and
scenes turned into question-and-answer exchanges with no stakes, no decisions, and no sense that
the world would move without the player.

## Design sources

The doctrine adapts three well-known bodies of GM advice. The summaries below are in our own words;
the links are the primary sources.

- **Don't prep plots, prep situations** (Justin Alexander, *The Alexandrian*). A plot is a sequence
  of events; a situation is a set of circumstances whose outcome depends on what the players do.
  Prepare the people, places, goals, resources, and what would happen without the party. React to
  the players instead of pre-writing their reactions.
  <https://thealexandrian.net/wordpress/4147/roleplaying-games/dont-prep-plots>
- **Node-based scenario design and the Three Clue Rule** (Justin Alexander). Treat scenes, places,
  and people as nodes in a web. For any conclusion the players must reach, plant at least three
  clues in different places. Invert the rule when designing: each node should offer several ways
  onward, so the players always have at least one live path even when they miss or misread
  something.
  <https://www.thealexandrian.net/creations/misc/node-design/node-design.html>
- **Encounters are dramatic scenes, not damage races** (The Angry GM). A good encounter has a
  dramatic question, a hook, a source of conflict, and enough decision points to keep the players
  choosing. Its structure keeps score so it can end. The opposition wants something and acts on it.
  <https://theangrygm.com/how-to-build-awesome-encounters/>

## The model

### 1. A situation, not a plot

A **situation** is the unit the GM records. It is stored in the existing storyline record (the name
is kept for compatibility) and has these parts:

| Field | Meaning | Visibility |
| --- | --- | --- |
| `name`, `summary` | What the situation is and what the player knows of it | Player-facing |
| `question` | The one dramatic question the situation asks, such as "Who took the miller's son?" | GM |
| `forces` | The people and powers in it, each with what it wants and what it does next | GM |
| `pressure` | What happens if no one interferes, and the next visible sign of it | GM |
| `plot` | The situation as it stands and what is at stake | GM |
| `characters`, `locations`, `objects` | The people, nodes, and clues in play | GM |
| `notes` | Anything else worth remembering | GM |

The question is the anchor. A situation is resolved when its question is answered, not when a
prepared scene list runs out. A force without a want and a next move is scenery; a pressure without
a visible sign is a gotcha.

### 2. A scene, not a narration

Every turn is a **scene**. The GM checks four things before recording changes:

1. **Dramatic question**: what does the player want in this beat?
2. **Source of conflict**: what stands in the way, visibly?
3. **Stakes**: what happens if the player fails or walks away?
4. **Decision point**: what are at least two meaningful things the player can do?

A scene with only one possible action is a cutscene. Cut to the choice instead of playing it out.
A scene that ends without a choice or a fresh complication is a summary, not a scene.

### 3. A web of nodes and clues

Places, people, and objects are **nodes**. Each node should offer a way onward to at least two other
nodes, and its secret should point somewhere. When the story needs the player to learn something,
plant at least three clues on different nodes. One missed clue, one failed roll, or one ignored NPC
must never be the only path forward.

This is the anti-railroad rule in mechanical form. The GM does not prepare a path; it prepares a
web and lets the player choose the route.

### 4. Pressure: the world moves first

Every active situation has **pressure**: what happens if no one intervenes, and the next visible
sign of it. When time passes or the player leaves a situation alone, the GM decides what its forces
did and records the next sign. The order is always:

1. Telegraphed sign. The player can see something is coming.
2. Clear consequence. A cost lands or a deadline closes.
3. Irreversible change. The situation resolves in a way the player did not choose.

Pressure is not a punishment for choosing a different scene. It is the world's own agenda. The
player may always intervene, and a reasonable plan must be able to change the outcome.

### 5. Encounters as scenes with stakes

An encounter is not a damage race. Before initiative the GM decides:

- What each side wants.
- What is at stake.
- How the fight can end besides one side dying: driven off, captured, bargained with, fled, or the
  objective won.
- What the ground offers: cover, height, hazards, chokepoints, objects worth using.
- What changes at the midpoint: reinforcements, fire, terms, a route opening or closing.

Foes act on what they want. They take cover, protect the objective, call for help, flee when broken,
and surrender when it is hopeless. A clever plan, a trap, or a parley is a way to win, not a way to
skip the fight.

## The turn loop

The GM runs this loop inside the existing state step, before narration:

1. Read the player's action. Decide which situation it touches.
2. Name the scene's question and source of conflict. If there is no question, connect the beat to a
   live situation or cut to one.
3. Resolve the action honestly with the engine's rolls and tools. A failure changes direction; it
   does not stop the story.
4. Move the world: decide what the situation's forces do next, and show the first sign of pressure.
5. Record the changes: the player-facing summary, the question if it changed, the forces' next
   moves, the pressure, and any resolved or deactivated situation.
6. End the beat on a choice or a fresh complication.

The GM never pre-scripts an outcome, never makes a committed plan fail for a reason invented
afterward, and never chooses the player's next action.

## Focus, limits, and resolution

- One situation is **focused**: the one the player is engaging now. The others are secondary and
  appear as a sign of pressure at the edge of a scene or in a mention.
- At most three situations are active: one focused and up to two secondary. More than that dilutes
  the story and the GM's attention.
- A situation is **resolved** when its question is answered, even if the answer is bad for the
  player. It leaves the active list and stays searchable.
- A situation that only pauses is **deactivated**, not resolved.
- A situation that splits into two distinct threads becomes an update plus a create in the same
  reply.

## How the engine implements it

- **Data**: the storyline record now carries `question`, `forces`, and `pressure` alongside the old
  fields. All new fields are optional on replay, so campaigns recorded before this change still
  load and export.
- **Tools**: `create_storyline` requires the question, forces, and pressure, so the GM cannot create
  an empty thread by habit. `update_storyline` accepts the same fields and treats an omitted field
  as unchanged and an empty value as cleared. `search_storylines` searches all of them.
- **Orientation**: every state step lists the active situations, focused first, with their question,
  situation, forces, pressure, cast, nodes, objects, and notes. The GM re-reads the live situation
  every turn instead of relying on the summary alone.
- **Doctrine**: the core `storylines` rule defines the situation model. The `story_craft` fragment
  teaches the scene loop, the three-clue rule, and the node web. The `encounter_craft` fragment
  teaches encounter design where the rule set offers combat.
- **Checklist**: the `storylines` checklist item asks what the forces did, which pressure sign the
  player saw or missed, and what the action changed. This is the reminder that survives long
  context windows.
- **Opening**: the opening records one situation for each distinct hook, with question, forces, and
  pressure, and focuses the one the opening starts with.

## Worked example: SRD 5.2.1 (5E compatible)

**Situation: The Missing Tithe.** The village of Sallow Ford has not paid its tithe to the abbey
for two seasons. The abbey's steward is coming to seize the mill, and the reeve is hiding why the
money is gone.

- **Question**: Who took the tithe, and can the village pay before the steward arrives?
- **Forces**: The reeve wants the theft hidden and will burn the ledger; the miller's widow wants
  the debt paid and will sell the mill; the abbey steward wants the money and does not care who
  suffers for it.
- **Pressure**: In three days the steward arrives with soldiers; first, a rider comes ahead to
  count the grain.
- **Nodes and clues**: the mill (a false bottom in the grain bin), the reeve's house (a burned
  ledger with one surviving page), the abbey road (the rider's sealed writ), the widow (what she
  saw the night the money left), the ferryman (who crossed with a heavy chest).
- **Scenes**: the rider's arrival offers a choice between bribing, delaying, or following him; the
  mill offers a choice between searching openly or asking the widow; the reeve offers a choice
  between pressure, a bargain, or a watch.
- **Encounter**: if the reeve's men try to burn the ledger, the fight is about the fire and the
  document, not about killing everyone. The men surrender when the ledger is saved and the reeve
  is named; the steward's soldiers arrive as a new pressure if the village cannot pay.

The GM does not know whether the player will expose the reeve, pay the tithe, flee with the widow,
or side with the abbey. It knows what each force will do next and what the player can find where.

## Worked example: Hordes

**Situation: The Mercy Street Pharmacy.** The party needs insulin from a pharmacy two blocks into
a contested street. A survivor crew has already fortified the front and is using the back loading
bay; a horde is drifting up the street from the south.

- **Question**: Can the party get the insulin and get out before the street fills with the dead?
- **Forces**: The crew wants the pharmacy's stock and will trade only from strength; the horde
  follows noise and will arrive in force once the front is breached; one crew member wants to
  defect and will help for a share.
- **Pressure**: The horde reaches the block in four scenes; first, a stray walks into the front
  lot and the crew argues about shooting it.
- **Nodes and clues**: the front counter (stock list), the loading bay (the crew's guard), the
  back alley (a drain entrance), the roof (a way over the street), the defector (a back door and
  the crew's watch rotation).
- **Scenes**: a negotiation at the barricade, a quiet search of the aisles with the horde visible
  through the windows, a chase across the roof, or a run through the storm drain.
- **Encounter**: noise is the real enemy. Every gunshot raises the `noise` clock and the
  `city_alert` clock; the crew breaks when its leader falls or when the horde reaches the door, and
  the horde's arrival is a change in the situation, not a second fight bolted on.

The GM records the clocks and the situation's pressure separately: clocks are the engine's visible
track, the pressure line is the fiction's next move.

## Anti-patterns and their fixes

| Anti-pattern | What it looks like | Fix |
| --- | --- | --- |
| Prepped plot | "First the party meets the steward, then the widow, then the abbey." | Record forces and nodes, not a scene order. |
| Single clue | The only way to learn the truth is one roll in one room. | Plant three clues on different nodes. |
| Filler encounter | A fight with no goal, stake, or alternative. | Give it a dramatic question, an objective, and a way out. |
| Frozen world | Nothing happens until the player acts. | Advance pressure when time passes, with a visible sign first. |
| Gotcha pressure | The first sign of trouble is the disaster itself. | Telegraph, then escalate. |
| Nullified plan | The player's preparation fails for a reason invented afterward. | Honor the plan; let it fail for a reason the player could see. |
| Summary scene | The beat ends with "what do you do?" and no change. | End on a choice or a fresh complication, with a stake visible. |
| Endless fight | The fight continues after its question is answered. | Let foes flee, surrender, or be driven off; end the scene. |
| Omnipotent villain | Every player move is countered by the villain's perfect knowledge. | Play each force with its own information and limits. |

## Live-test protocol

The doctrine is judged in play, not in the prompt text. The test protocol is:

1. Fresh database and server; DeepSeek as the provider.
2. One SRD 5.2.1 campaign and one Hordes campaign, played for many turns with mixed scenes: social,
   investigation, travel, a battle, a negotiation, and a quiet beat.
3. After every few turns, inspect the recorded storylines: does each active situation have a real
   question, forces with next moves, and pressure? Did the GM update them when the fiction moved?
4. Score each turn on: a visible dramatic question, at least two meaningful choices, a consequence,
   a pressure sign, and no invented outcomes.
5. Iterate the prompts when a failure repeats; rerun the same kind of scene.

### Results from the design pass

Two live campaigns were played with DeepSeek and inspected through the event log.

**SRD 5.2.1 (5E compatible), Unwritten World, 16 turns.** The opening recorded three situations,
each with a real question, forces with next moves, and pressure: *The Debt at the Turn of the
Moon* (a charter debt read in three days), *The Wool Merchant's Errand* (an empty tower that
someone wants left alone), and *The Shrine Thief and the Scraped Names* (names scraped from the
record the town needs). Observed in play:

- Clue-bearing nodes fed both investigations: the petition in the inn's flour drawer and the old
  land papers served the charter debt, while the painted names on the shrine porch and the clerk's
  slips tied the scrape to Colde. Later, the miller's dropped slip, a second list behind the
  wheel-house frame, and the miller's own account formed a second three-clue chain to Verrin Colde.
- The clue web crossed situations: the shrine thief's payer, Colde, was also Orrin Vell's rival in
  the wool merchant's errand, so the two threads met without either being scripted to.
- Pressure moved the story. The deadline became a real morning; the assessor took depositions at
  first light, and Colde arrived with a sealed commission. The GM did not wait for the player to
  ask for the scene.
- Decisions carried weight: bargain with the miller or take him in; open the wheel-house; accuse
  Colde publicly or wait for the law; bring the miller to the assessor before Colde could leave.
- Failure changed direction rather than stopping play. A failed Stealth check exposed the watch and
  let the suspect flee; a failed chase put him behind a barred mill door; the player had to find
  another way in, which led to the second slip.

The first turn showed one weakness: the GM learned the assessor's standard of proof but did not
update the situation's player-facing summary until later. The checklist and the "pair information
with a cost, threat, or choice" wording were added after this run.

**Hordes, Unwritten Apocalypse, 10 turns.** The opening recorded three situations: *The Cut Seal*
(who cut the barricade seal and when they will return), *The Water Debt* (how the block pays for
water before the stores run out), and *The Line in the Water* (where the dead are walking and what
they walk away from). Observed in play:

- The site tools and city navigation carried the situation: the GM looked up a pharmacy site,
  created it, searched it, crossed the city by punt, raised the Noise clock with a 7-9 Scavenge
  consequence, and moved a threat toward the shop.
- The 7-9 consequence was a real decision point: time, noise, poor condition, or a complication.
  Choosing noise started the clock and brought the dead; choosing condition would have made the
  medicine unreliable.
- A 6- on the escape move produced a hard move with position, a condition, and a new NPC behind the
  stockroom door. The fight that followed dealt harm but stayed about the insulin, the door, and
  the tide rather than about killing every walker.
- The last exchange tested the encounter doctrine directly: a 7-9 dealt harm and pinned Mara, and
  a 6- Harm Move added panicked and 2 harm. The scene did not become a damage race; it became a
  choice about whether Maya opens the stockroom door while the van's horn pulls the line up
  Meridian Street.
- Clocks and situation pressure stayed distinct: the Noise and City Alert clocks are the engine's
  visible tracks; the pressure line is the fiction's next move.

The run also exposed two pre-existing Hordes rough edges. An `improve_site` call that needed an
item the character did not carry raised a plain `ValueError`, which the tool layer reports only as
"the tool failed"; it now raises `InvalidInput`, so the GM sees which item is missing and can
change plan. And the GM asked for Scavenge with Know-how; the engine refused and it retried with
Wits. That second one is recorded in `ISSUE_TRACKER.md`, because it belongs to the Hordes move
schema rather than the storyline system.

### What the live runs changed

- `create_storyline` requires the question, forces, and pressure, so the model cannot record a
  summary-only thread by habit.
- The turn orientation prints question, situation, forces, and pressure, so the GM re-reads the
  live situation every turn instead of relying on the public summary.
- The checklist asks what the forces did, which pressure sign appeared, and what changed.
- The `story_craft` prompt gained two lessons from play: an answer is not a beat by itself (pair it
  with a cost, threat, or choice), and a time jump must move every active situation, not only the
  focused one.
- The `story_craft` and `encounter_craft` fragments were confirmed in the live system prompts of
  both campaigns.

### What still needs watching

- Information-only beats: the GM still sometimes ends on an NPC's answer. The new wording pushes
  it to attach a cost or choice; more live runs should confirm the effect.
- Secondary situations can go quiet during a long scene. The time-jump rule is the first fix; a
  future engine pressure track may be a stronger one.
- The GM sometimes sequences a move and a speech awkwardly inside one turn and falls back on
  `force` for the speech. This is a pre-existing tool-order behavior, not a storyline issue.
- Day-length journal limits caused repeated retries on the first Hordes and SRD days. That is a
  separate journal-budget issue.

## Open questions

- Should a situation carry a small numeric pressure track (like the Hordes clocks) instead of free
  text? Free text is flexible but easier to forget.
- Should clues be a first-class field with explicit "leads to" links, or is the current
  places/objects/notes mix enough?
- How much pressure is too much before players feel punished for exploring?
- Can the GM learn to vary scene types reliably, or should the engine weight the checklist toward
  the scene type that has been absent longest?
- Would a per-situation "cast and agenda" table beat the current force strings for long campaigns?
