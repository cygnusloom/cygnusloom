# Hordes — ruleset specification

A Powered by the Apocalypse zombie-survival game for Cygnus Loom, set in the
blank `unwritten-apocalypse` world during the first winter after the dead rose.
The GM invents the region, the shelter, the survivors, and the trouble; the
ruleset supplies the procedures, the tables, and the engine-backed crunch.

- **Ruleset id:** `hordes`
- **World id:** `unwritten-apocalypse`
- **Status:** implemented 2026-11-24; generated site types, zone scavenging, and city navigation added 2026-11-25.
- **Design rule:** anything reusable belongs in the generic engine and is
  parameterized here. Only zombie-survival policy lives in the Hordes module.

## Sources and legal

- All rules text is original.

## Locked design

- **Dice:** 2d6 + stat + skill + situational modifier. 10+ full success, 7–9
  success with a cost, 6– the GM makes a move.
- **Stats:** Grit, Reflexes, Wits, Presence, Know-how. Base array −1, 0, 1, 1,
  2 arranged freely; each playbook adds +1 to its signature stat. Stats do not
  rise through play.
- **Skills:** ten skills at rank 0–2. Rank adds to the roll. Risky use marks
  practice; three marks raise rank 0→1, six total marks raise 1→2. Downtime
  training and teaching add marks. Skills are the character's growth.
- **Playbooks:** Soldier, Medic, Scavenger, Mechanic, Preacher, Cop. A playbook
  fixes a stat bonus, two trained skills, one starting knowledge move, and a
  starting kit. Moves are shared; playbooks do not own moves.
- **Knowledge moves:** Radio Operation, Computer Use, Cybernetics
  Configuration, Field Surgery, Vehicle Repair, Occult Lore, Demolitions,
  Chemistry. Anyone can learn one through teaching, a manual, or difficult
  experimentation. The engine refuses an unlearned knowledge move.
- **Harm:** a 0–6 track plus conditions. Weapons and threats have fixed harm
  ratings, not damage dice. Armor reduces harm; piercing ignores armor. At 4+
  the character is *wounded*; at 6 the character is dying.
- **Infection:** a rare complication of deep bites, dirty wounds, contaminated
  water or sites, and unsterile surgery. It is not automatic on every bite.
- **Cybernetics and occult relics:** cybernetics add **Rejection** (0–6);
  occult relics add **Dread** (0–6). Each track has thresholds, conditions, and
  treatment.
- **Hordes:** a horde is one threat with magnitude 1–5 and a size clock. It
  attacks as a swarm, follows noise, and can split into individual zombies when
  the fiction zooms in. Other threats include eldritch entities, strange
  horrors, infected animals, and hostile survivors.
- **Combat:** PbtA conversation, not D&D turns. No initiative, no rounds, no
  turn order, no grid requirement, and no monster attack rolls. See *Combat*
  below.
- **Survival crunch:** ammunition and reloads, firearm ranges and noise,
  vehicles, food/water/shelter, medicine and infection, crafting/repair,
  barter/scarcity, and base building are all in scope and engine-backed.
- **Exploration:** site-based scavenging for scientific items, cybernetics,
  occult relics, and knowledge loot, with rarity and condition.
- **NPCs:** the AI GM runs named agendas, companions, rival crews, off-screen
  factions, and settlement life. The engine supplies clocks, faction turns,
  follower sheets, and base state, not a full simulation.
- **Progression:** better gear, better skills, more knowledge, stronger allies,
  and a better base. No XP and no levels.
- **World:** `unwritten-apocalypse` is description-only. The GM generates the
  cause, region, shelter, factions, sites, and omens from the setting tables.

## The first winter

Play begins a few months after the fall. Institutions are gone, the first
winter is closing in, and the dead are the weather. The cause is unwritten:
the setting tables decide whether it was a lab leak, a plague, a ritual, a
strange rain, or something nobody agrees on. The world description is short:

> A modern world in its first winter after the dead rose. The cause is unknown.
> The old systems are gone. Survivors hold a few shelters, scavenge the ruins,
> and listen for the horde. The GM invents the region, the shelter, the
> survivors, and the trouble from the setting tables.

Opening procedure:

1. The GM rolls the setting tables (`apocalypse-cause`, `region`, `season`,
   `shelter`, `settlement`, `faction`, `threat`, `omen`) with
   `roll_creative(role="setting")`.
2. The GM records the world with `record_setting`: cause, region, season,
   shelter, the nearest settlement, the local power, the local threat, and the
   first omen.
3. The GM creates the starting shelter as a location and one or two named NPCs
   who share it.
4. The shelter starts with one fragile improvement (barricade, cellar, or
   watch post) and stores for a few days.
5. The opening ends with a concrete pressure: a noise in the night, a
   neighbour's absence, a horde on the road, or a shortage that cannot wait.

## Characters

### Stats

| Stat | Covers | Typical moves |
|------|--------|---------------|
| Grit | endurance, nerve, holding on | Engage (when brawling), Defend, Harm Move, Resist Infection |
| Reflexes | speed, aim, reaction | Shoot, Sneak, Flee, Drive, Athletics |
| Wits | attention, memory, cunning | Assess, Scavenge, Forage, Track, Occult Lore |
| Presence | will, leadership, empathy | Persuade, Command, Trade, Face Horror |
| Know-how | craft, repair, medicine, tech | Fix, Treat, Craft, Salvage, knowledge moves |

Base array: −1, 0, 1, 1, 2. Each playbook adds +1 to its signature stat. The
result is a PbtA spread such as +2, +1, +1, 0, −1. There is no stat growth;
gear, skills, knowledge, and allies are the progression.

### Skills and practice

| Skill | Stat | Covers |
|-------|------|--------|
| Firearms | Reflexes | shooting, reloading, weapon care |
| Melee | Grit | hand weapons, brawling, holding a line |
| Athletics | Grit | climbing, running, forcing, swimming |
| Stealth | Reflexes | sneaking, hiding, ambushing |
| Scavenging | Wits | searching sites, spotting loot, salvage |
| Survival | Wits | foraging, tracking, weather, shelter |
| Mechanics | Know-how | repairs, crafting, vehicles |
| Medicine | Know-how | wounds, surgery, infection, medicine |
| Driving | Reflexes | vehicles, chases, road hazards |
| Persuasion | Presence | parley, trade, command, morale |

- Rank 0 is untrained and adds +0. Rank 1 is trained and adds +1. Rank 2 is
  expert and adds +2. The cap is 2.
- A move that names a skill marks one practice tick when it is rolled under
  risk. One tick per skill per scene. A 6– also marks the skill.
- Rank 1 costs 3 ticks; rank 2 costs 6 ticks total. When the threshold is
  reached, the rank rises and the ticks reset. Ticks and ranks are engine
  state, not player bookkeeping.
- Downtime training, a teacher, a manual, or a dedicated practice scene adds
  ticks without a roll. The GM may require supplies, time, or a check.
- Creation: a playbook grants two trained skills (rank 1). Everyone else is
  rank 0. Knowledge moves are separate and binary.

### Playbooks

| Playbook | +1 | Trained skills | Starting knowledge move | Kit |
|----------|----|----------------|-------------------------|-----|
| Soldier | Grit | Firearms, Melee | Radio Operation | rifle, pistol, ballistic vest, knife, two magazines, 20 rounds, helmet, rations |
| Medic | Know-how | Medicine, Survival | Field Surgery | medkit (5 uses), pistol, bandages, antibiotics, morphine, water |
| Scavenger | Wits | Scavenging, Stealth | Computer Use | crowbar, light pistol, large pack, lockpicks, flashlight, 10 rounds |
| Mechanic | Know-how | Mechanics, Driving | Vehicle Repair | toolkit, shotgun, fuel can, parts, wrench, 8 shells |
| Preacher | Presence | Persuasion, Survival | Occult Lore | a relic, pistol, holy book, candles, salt, 6 rounds |
| Cop | Reflexes | Firearms, Persuasion | Radio Operation | pistol, ballistic vest, baton, radio, handcuffs, 12 rounds |

Each playbook also chooses a personal item and fills in a *drive* and a
*burden* (see *Character fields*). Playbooks are templates, not classes: they
give a starting position, not a move list.

### Knowledge moves

| Move | Stat | Use |
|------|------|-----|
| Radio Operation | Wits | call for help, intercept, jam, reach a distant station |
| Computer Use | Know-how | recover data, hack, control a system, read a drive |
| Cybernetics Configuration | Know-how | install, calibrate, maintain, or disable an implant |
| Field Surgery | Know-how | stabilize, remove a bullet, amputate, install in the field |
| Vehicle Repair | Know-how | fix, modify, or jury-rig a vehicle |
| Occult Lore | Wits | identify a relic, rite, sign, or eldritch weakness |
| Demolitions | Know-how | place, disarm, or improvise explosives |
| Chemistry | Know-how | drugs, fuel, purification, poisons, medicine |

A knowledge move is learned, not assumed. Starting playbooks grant one.
Others are learned from a teacher, a manual, a working example, or difficult
experimentation: a project with time, materials, and a roll. The engine records
the learned move on the character and refuses `roll_move` for a character who
has not learned it.

### Character fields

- **Drive:** what keeps you going (protect someone, find a cure, build
  something that lasts, see the world that comes after).
- **Burden:** what you carry (a debt, a wound, a person, a secret, a relic).
- **Scar:** the mark the first winter left on you.
- **Look:** one concrete visual detail.
- **Background:** what you did before.
- **Shelter duty:** what you do at the base (watch, cook, fix, teach, tend).
- **Personal item:** one thing that is yours.

Drive, burden, scar, look, background, shelter duty, and personal item are
flavor fields: a player may rewrite them, and a hireling's flavor belongs to
the GM. Stats, skills, knowledge moves, harm, and gear are mechanical.

### Creation

- One creation layout: Identity, Playbook, Stats, Details, Gear, Knowledge.
- Standard characters: one per playbook, prefilled with stats, skills,
  knowledge move, kit, drive, burden, and a suggested name.
- Starting gear kits are item grants, not free shopping. A kit's weapon comes
  loaded, with the listed spare ammunition.
- The character sheet shows harm (0–6), conditions, skills and practice ticks,
  knowledge moves, loaded rounds per firearm, inventory, currency-less trade
  goods, and the base.

## Moves

All moves are shared. A move is a procedure, not a permission: if the fiction
does not support it, the GM says what would be required and asks again.

| Move | Stat | Skill | Trigger |
|------|------|-------|---------|
| Assess | Wits | — | study a place, person, or threat |
| Sneak | Reflexes | Stealth | move unseen or unheard |
| Scavenge | Wits | Scavenging | search a site for supplies |
| Forage | Wits | Survival | find food and water in the wild |
| Track | Wits | Survival | follow a trail or read the ground |
| Fix | Know-how | Mechanics | repair or jury-rig gear |
| Treat | Know-how | Medicine | treat a wound or condition |
| Persuade | Presence | Persuasion | talk someone into something |
| Command | Presence | Persuasion | order followers or civilians |
| Trade | Presence | Persuasion | barter goods or services |
| Engage | Grit or Reflexes | Melee | fight hand to hand |
| Shoot | Reflexes | Firearms | fire a ranged weapon |
| Defend | Grit | Melee or Athletics | hold a line, protect someone, get clear |
| Defy Danger | by approach | chosen skill | react to immediate danger |
| Flee | Reflexes | Athletics or Driving | run, drive, or slip away |
| Drive | Reflexes | Driving | control a vehicle under pressure |
| Craft | Know-how | Mechanics | make something from parts |
| Salvage | Wits | Scavenging | strip a site or wreck for parts |
| Recuperate | Grit or Presence | Survival or Medicine | rest and recover under pressure |
| Harm Move | Grit | — | take harm |
| Resist Infection | Grit | Survival | a deep bite, dirty wound, or contamination |
| Face Horror | Presence | — | confront an eldritch entity or horror |
| Order Follower | Presence | Persuasion | send a follower into danger |
| Train | Know-how or Wits | — | downtime learning |

Outcome bands, in the shared PbtA form:

- **Assess:** 10+ ask two true questions; 7–9 ask one, and the answer costs
  time, noise, or exposure; 6– the GM reveals a danger or you miss something.
- **Sneak:** 10+ get there unseen; 7–9 get there but leave a sign, are seen, or
  lose something; 6– caught, cornered, or ambushed.
- **Scavenge:** 10+ find what you need and something extra; 7–9 find something
  but choose time, noise, condition, or a complication; 6– nothing useful, or a
  hazard.
- **Forage:** 10+ food and water for the day and extra; 7–9 one of the two, or
  both with a cost; 6– nothing, or something contaminated.
- **Track:** 10+ follow the trail and learn something; 7–9 follow it but lose
  time or take a risk; 6– lose it or walk into danger.
- **Fix:** 10+ it works, and works better than before; 7–9 it works but needs
  parts, time, or a condition; 6– it breaks worse.
- **Treat:** 10+ clear harm or a condition; 7–9 stabilize or clear one with a
  cost; 6– it worsens, or infection risk.
- **Persuade:** 10+ they agree; 7–9 they agree with a price, favour, or
  condition; 6– they refuse, take offence, or raise the price.
- **Command:** 10+ they act; 7–9 they act with hesitation or a cost; 6– they
  refuse, freeze, or panic.
- **Trade:** 10+ a fair deal and something extra; 7–9 a deal with a catch;
  6– no deal, or a bad reputation.
- **Engage:** 10+ deal harm and keep control; 7–9 deal harm and suffer a
  consequence; 6– miss, and the GM makes a hard move.
- **Shoot:** 10+ hit and choose an effect (pin, drive back, break cover);
  7–9 hit but spend extra ammo, expose yourself, or take a consequence;
  6– miss, jam, or run dry.
- **Defend:** 10+ hold, redirect, or get someone clear without harm; 7–9 hold
  but take harm or a cost; 6– overrun.
- **Defy Danger:** 10+ avoid it; 7–9 avoid the worst and choose a cost; 6– you
  suffer it, and the GM makes a hard move.
- **Flee:** 10+ escape; 7–9 escape with a cost; 6– trapped, separated, or
  worse.
- **Drive:** 10+ control the vehicle; 7–9 control it with a cost; 6– crash,
  stall, or lose control.
- **Craft:** 10+ it works; 7–9 it works with a flaw, shortage, or shorter life;
  6– materials wasted, or a dangerous failure.
- **Salvage:** 10+ parts and something extra; 7–9 parts with a cost; 6– nothing
  useful, or a hazard.
- **Recuperate:** 10+ clear harm/conditions; 7–9 clear some with a cost;
  6– rest is interrupted or does not take.
- **Harm Move:** see *Harm*.
- **Resist Infection:** see *Infection*.
- **Face Horror:** 10+ hold yourself together; 7–9 mark Dread or take a
  condition; 6– panic, Dread, or a hard move.
- **Order Follower:** 10+ they do it; 7–9 they do it with hesitation or a cost;
  6– they refuse, freeze, or break.
- **Train:** 10+ learn the knowledge move or add practice; 7–9 progress with
  time, cost, or a partial understanding; 6– no progress, or a dangerous
  mistake.

Knowledge moves use the same bands with their own triggers and outcomes.

*Defy Danger* chooses the stat and the skill that fit the approach (for example
Reflexes + Stealth to dodge behind a car, or Grit + Athletics to hold a door).
The proposed `skill_choice` move support lets `roll_move` add the chosen skill
and mark the chosen skill for practice.

## Combat is a conversation, not a mode

Hordes deliberately does not use the engine's turn-based encounter system. A
fight is the same conversation as the rest of play.

### The flow

1. The GM describes the situation and makes a **soft move**: trouble that is
   coming, not yet landed. "The lead runner breaks from the alley, arms wide.
   What do you do?"
2. The player describes what their character does.
3. If the action triggers a move, the GM calls `roll_move`. The player rolls
   2d6; the engine adds the stat, skill, conditions, and any situational
   modifier.
4. The band resolves the action. On a 6–, the GM makes a **hard move**. The
   situation snowballs; the spotlight moves to whoever the fiction follows.
5. Repeat. There is no round, no order, and no "end turn" button.

### What this means in play

- **No initiative.** The fiction is the initiative. A character who is ready,
  hidden, or acting first simply acts. A character who is pinned, surprised, or
  at the bottom of a stairwell does not.
- **No rounds.** Time passes by the fiction, not by a six-second tick. A
  reload, a sprint, and a shouted warning can all happen in the same beat if
  that is what the moment holds.
- **No grid requirement.** Ranges are fictional tags: *close*, *near*, *far*,
  *out of reach*, *behind cover*, *upstairs*, *across the street*. A map may be
  drawn as a visual aid, but it never imposes turns or movement budgets.
- **Monsters do not roll.** A threat does not make an attack roll. When it
  attacks, the GM makes a move: describe the attack, then ask what the player
  does. The character suffers harm when they ignore a soft move, when their own
  move says they do, or when they roll a 6–. The engine applies the harm; the
  GM does not invent it.
- **Fictional positioning decides what is possible.** Cover, height, darkness,
  noise, who is holding what, and how many threats are on whom all change what
  move triggers and what a 7–9 costs. A foe that cannot be hurt by a knife
  simply does not trigger *Engage*; the GM says what would be required.
- **Soft moves constantly.** After every player move, the GM describes the
  changed situation and asks someone what they do. The dead do not stand
  around waiting to be attacked.
- **Do not trade blows.** Harm is small and consequences are concrete. A fight
  should change the situation on every roll: positions, noise, ammo, cover,
  injuries, who is in danger, what is on fire.
- **End fights early.** When the outcome is clear, threats flee, surrender,
  drag their wounded away, or are simply overrun. A fight is not a race to
  zero harm.
- **Zoom.** A group of threats is one threat. When the fiction focuses on
  individuals, split the group into individual threats. When the individuals
  are no longer the focus, merge them back. The spotlight follows the zoom.
- **Everyone can act.** Non-combat characters assess, treat, repair, drive,
  persuade, barricade, or rescue. A fight with no one to shoot is still a
  fight.

### Engine conformance

- `combat.mode: "moves"` (proposed) suppresses `start_encounter`,
  `begin_turn`, `end_turn`, `attack`, `player_attack`, `resolve_attacks`,
  `stage_encounter`, `place_participant`, and initiative. The Hordes campaign
  has no `encounter.started`, `combat.turn_begun`, or attack budget.
- `roll_move` is available anywhere, including during a fight. It is the only
  player-facing resolution. The engine adds stat, skill, conditions, and the
  GM's situational modifier, and records practice.
- Move bands can apply `harm`, `ammo`, `noise`, `clock`, and `condition`
  effects to a target. The engine applies armor, piercing, and thresholds.
- The GM applies harm from a threat with `harm_character` or `harm_threat`.
  Neither tool requires a turn or an encounter.
- `move_actor` moves NPCs and threats without turns. Clocks and faction turns
  handle off-screen action.
- Maps and `place_map_token` remain optional visual aids. They never gate an
  action by movement or order.
- A conformance test asserts that a complete fight produces no encounter,
  initiative, or turn events, and that no threat made an attack roll.

## Harm, conditions, and death

### The harm track

Harm is a 0–6 track on every player character and important NPC. It is a
counter with thresholds, not hit points.

| Harm | State |
|------|-------|
| 0 | fine |
| 1–3 | hurt; no penalty |
| 4–5 | **wounded** (−1 to physical rolls) |
| 6 | **dying** (death clock 3; can be stabilized) |
| 7+ | dead, or dying and fatal |

- Harm is a fixed rating. There are no damage dice and no hit-point attrition.
- Armor subtracts from harm. *Piercing* ignores armor. Cover, called shots,
  fire, explosions, and being surrounded adjust harm by 1.
- A character at 4+ harm is *wounded*. A character at 6 is dying; a dying
  character cannot act except to crawl, speak, or use a last action the GM
  allows.
- **Harm Move:** when a character takes 2+ harm in one hit, or any harm while
  already wounded, roll +Grit. 10+ keep your feet and act; 7–9 choose: drop what
  you are holding, lose your position, hesitate, or take a condition; 6– the
  GM makes a hard move (worse harm, separated, captured, gear lost).
- **Recovery:** a safe rest clears 1 harm; a full day with food, water, and
  shelter clears 2; `Treat` clears 1 harm and one condition per medkit use;
  field surgery clears 2 harm and a broken condition. Infection, Rejection,
  and Dread have their own treatment.
- **Death:** at 6 harm the character is dying and a three-segment death clock
  starts. A successful stabilize or field surgery stops it. If it fills, the
  character dies. The player gets last words and a final action. A replacement
  character joins at the next shelter; the table decides what they carry and
  what the shelter lost.

### Conditions

Conditions are concrete states, not modifiers to memorize. Each has a source,
an effect, and an end trigger.

| Condition | Effect | Ends |
|-----------|--------|------|
| Bleeding | 1 harm per scene until treated | Treat, rest |
| Wounded | −1 to physical rolls | harm below 4, rest |
| Broken | a limb is useless; −2 to actions using it | surgery, time |
| Stunned | −1 to all rolls; cannot defend | scene |
| Pinned | cannot move; −1 to all rolls | escape, scene |
| Panicked | −1 Presence and Wits | calm, scene |
| Exhausted | −1 to all rolls | rest |
| Dehydrated | −1 Reflexes | water |
| Starving | −1 Grit | food |
| Fever | −1 to all rolls | medicine, time |
| Infected | infection clock advances | treatment |
| Reeling | −1 Wits | rest, calm |
| Shaken | −1 Presence | safety, rest |
| Blinded | −2 to sight-based rolls | scene, aid |
| Deafened | cannot hear; −1 to awareness | aid, time |
| Prone | −1 to melee; movement costs | stand up |
| Hidden | +1 to Stealth; attacks from hiding +1 harm | move, attack |
| Aiming | +1 to the next shot | shot, scene |
| Inspired | +1 to one roll | used, scene |
| Marked | a horror knows where you are | ritual, distance |
| Rejected | −1 Know-how; at 6, dying | treatment |
| Addicted | without the substance, −1 to all rolls | treatment, time |

Conditions are engine state and appear on the sheet. The GM adds them with
`add_condition`; they are not free narration.

## Infection, medicine, Rejection, and Dread

### Infection

Infection is a rare complication, not the default result of a bite.

- **Exposure:** a deep bite, a dirty wound, drinking contaminated water,
  breathing a contaminated site, or surgery without sterile tools.
- **Roll:** `Resist Infection` (+Grit, Survival). 10+ no infection; 7–9 the
  clock starts at 1; 6– it starts at 2.
- **Infection clock (0–4):** 1–2 no penalty; 3 fever (−1 all); 4 turning
  (fever, delirium, dying).
- **Treatment:** antibiotics, clean surgery, and rest reduce the clock by 1–2.
  Amputation at 2–3 removes the source but leaves a *broken/maimed* condition.
  A rare scientific item (antiviral, experimental serum) can cure late-stage
  infection. Nothing mundane cures it at 4.
- Bites are still dangerous: they cause harm and bleeding. The infection roll
  is a complication, not a second attack.

### Medicine

- Medkits have uses; bandages, antiseptic, antibiotics, painkillers, morphine,
  adrenaline, insulin, blood bags, and antivirals are separate items.
- `Treat` covers first aid, clearing harm, and stabilizing. `Field Surgery`
  covers bullets, broken bones, amputations, and implant installation.
- Medicine is scarce. Every use is a choice; a medkit is loot, not a reset
  button. Painkillers and stimulants can add an *Addicted* condition.
- Rest, food, water, and shelter are the main recovery. There is no magical
  healing.

### Rejection (cybernetics)

- Each implant has a Rejection value 1–3. Installing it adds that much
  Rejection (0–6).
- Thresholds: 3 fever and tremor (−1 Know-how); 5 seizure (condition, −1 all);
  6 the body rejects the implant (dying, or the implant fails and must be
  removed).
- Treatment: anti-rejection medicine (rare), downtime, maintenance, or removal
  surgery. Every implant needs power or maintenance; a dead implant is inert.
- Installation uses `Cybernetics Configuration` and `Field Surgery`. A clean
  clinic and sterile tools are safer; a field install is a 7–9/6– waiting to
  happen.

### Dread (occult relics)

- Each relic has a Dread value 1–3. Attuning or using it adds that much Dread
  (0–6).
- Thresholds: 3 nightmares (−1 Presence); 5 obsession or compulsion (−1 all,
  condition); 6 the character breaks (GM takes over, the character retires, or
  a *Marked* condition follows them).
- Treatment: rest, community, ritual, or giving up the relic. Relics can be
  discarded, but the attention they brought may not leave.
- `Face Horror` marks Dread when a character confronts an eldritch entity or
  strange horror. `Occult Lore` identifies what a relic is and what it wants.

## Firearms, ammunition, and noise

### Weapons

Harm values are fixed. Range is a fictional band; the GM sets it from the
fiction.

| Weapon | Harm | Range | Tags | Ammo |
|--------|------|-------|------|------|
| Unarmed | 1 | close | — | — |
| Knife | 1 | close | quick, concealable | — |
| Bat, crowbar | 2 | close | heavy | — |
| Axe | 3 | close | heavy, slow | — |
| Spear | 2 | close/near | reach | — |
| Pistol | 2 | close/near | loud 1, concealable | 12 (9mm) |
| Revolver | 2 | close/near | loud 1, reliable | 6 (.38) |
| Shotgun | 3 close / 1 near | close/near | loud 2, spread, slow reload | 5 (12ga) |
| Rifle | 3 | near/far | loud 2, scoped | 5 (.308) |
| SMG | 2 | close/near | loud 2, auto | 30 (9mm) |
| Assault rifle | 3 | near/far | loud 3, auto | 30 (5.56) |
| LMG | 4 | near/far | loud 3, auto, heavy | 100 (5.56) |
| Bow | 2 | near/far | silent, slow | arrows |
| Crossbow | 3 | near | silent, slow reload | bolts |
| Molotov | 3 | near | fire, loud 2, area | — |
| Grenade | 4 | near | loud 4, area | — |

- Armor: leather jacket 1, ballistic vest 2, riot gear 3, plate carrier 2,
  helmet +1 to the head. Piercing ignores armor.
- A called shot adds +1 harm and a complication. Fire adds +1 harm and a
  condition. Being surrounded adds +1 harm.

### Ammunition

- Each firearm has a capacity and a loaded count. The sheet shows
  `loaded / capacity` and the caliber.
- Ammo items are bundles with uses: a box of 9mm has 20 uses, a box of 12ga
  shells 10, a box of .308 10, a box of 5.56 20. Reloading spends rounds from a
  matching bundle and takes a moment; under pressure it invites a GM move.
- Firearms jam on a 6–, or when the fiction says the weapon is abused. Clearing
  a jam takes a moment and a roll only if the situation is dangerous.
- Suppressors reduce noise by 1; they do not make a gun silent.
- Ammunition is loot and barter, not a catalogue purchase. Calibers matter.

### Noise

Noise is a shared 0–6 clock per scene or site. It is visible to the players:
they can see how loud they have been.

| Source | Noise |
|--------|-------|
| Quiet work, whispering, walking | 0 |
| Bow, crossbow, a quiet hand tool | 0 |
| Breaking glass, shouting, a slammed door | +1 |
| Pistol, suppressed rifle | +1 |
| Rifle, shotgun, engine, alarm | +2 |
| Automatic fire, explosion, fire engine, screaming | +3–4 |
| A screamer's scream | +3 |

- At 3 the dead stir: distant moans, movement at the edge of the scene.
- At 4 nearby hordes turn toward the noise.
- At 5 a horde is close; the next loud action may bring it.
- At 6 a horde arrives (or the current horde grows).
- Quiet time, distance, and closed doors reduce the clock by 1 per scene or
  significant pause. The GM records changes with `advance_noise`; the engine
  keeps the clock for horde movement.

## Hordes and other threats

### The horde

A horde is one threat with magnitude 1–5. It is not a character and does not
take turns.

| Magnitude | Harm capacity | Armor | Fiction |
|-----------|---------------|-------|---------|
| 1 | 3 | 0 | a knot of dead; can split into a handful |
| 2 | 4 | 0 | a street full; a real danger |
| 3 | 5 | 1 | a block; walls and noise matter |
| 4 | 6 | 1 | a district; a siege |
| 5 | 8 | 2 | a tide; the weather |

- A horde has tags (`horde`, `relentless`, `loud`, `slow` or `fast`) and moves:
  *surge*, *surround*, *drag down*, *break through*, *feed*, *call*, *split*,
  *merge*.
- **Attacks:** a horde does not roll. When it reaches a character, the GM makes
  a move and `threat_harm` applies harm (3 at magnitude 1–2; +1 per magnitude
  above 2). A character who `Defends`, `Defies Danger`, or gets clear may avoid
  it. A character who ignores the soft move is taken.
- **Splitting:** a horde at magnitude 1, or one that takes 3+ harm in a single
  blow, can split into individual zombies (1d6+2 walkers/runners). Individual
  zombies are threats with harm capacity 2. Four or more individual zombies in
  one place can merge into a magnitude-1 horde.
- **Driving it off:** fire, noise, a choke point, a vehicle, or a building can
  turn a horde. Harm reduces its magnitude or its capacity; the fiction decides
  whether it breaks, goes around, or keeps coming.
- **Horde pressure:** a campaign clock tracks the nearest horde's attention
  over days. It rises with loud scavenging, fires, and deaths; it falls with
  quiet, distance, and bait. At 6, the horde reaches the shelter or the current
  site.

### Other threats

Threats are not NPCs. They have harm capacity, armor, tags, and moves, and the
GM runs them with moves, not turns. Named survivors remain NPCs with full
sheets. A single-monster threat is shown and opened like any other monster: it
appears in the place's list and its statblock gives the harm capacity, armor,
tags, and moves. A threat that is a scale of its own, like a horde, is kept as a
brief line under the map with its name and where it is.

| Threat | Harm capacity | Armor | Tags | Moves |
|--------|---------------|-------|------|-------|
| Walker | 2 | 0 | shambling, relentless, infection-risk | grab, bite, moan |
| Runner | 2 | 0 | fast, fragile | sprint, leap, drag down |
| Crawler | 1 | 0 | low, quiet | grab ankle, crawl |
| Bloater | 3 | 1 | swollen, burst | burst, stagger |
| Spitter | 2 | 0 | ranged, corrosive | spit, retreat |
| Brute | 4 | 2 | strong, slow | smash, throw |
| Screamer | 1 | 0 | loud, fragile | scream, flee |
| Swarm | 2 | 0 | swarm, small | engulf, crawl |
| Horde | 3–8 | 0–2 | horde, relentless, loud | surge, surround, drag down, break through, feed, call |
| Eldritch entity | 3–5 | 2 or immune | eldritch, incomprehensible, dread | whisper, loom, unmake |
| Strange horror | 3–5 | 1–3 | unique, fear, dread | stalk, warp, feast |
| Raider | 2 | 1 | armed, cunning | shoot, flank, demand |
| Militia | 3 | 2 | disciplined, armed | volley, advance, hold |
| Cultist | 2 | 0 | fanatic, occult | chant, knife, summon |
| Infected dog | 2 | 0 | fast, pack | chase, maul |
| Bear | 4 | 1 | strong, territorial | maul, charge |

- **Eldritch entities** are immune to mundane harm unless the characters find
  the right weakness (salt, iron, fire, a name, a rite). They cause Dread and
  cannot be reasoned with. They are a mystery first and a fight second.
- **Strange horrors** are unique. A horror may have no harm capacity at all
  until the characters learn how to hurt it. Its moves change the world, not
  just the fight.
- **Survivors** are people. A named survivor uses the NPC system; a faceless
 band of raiders, militia, or cultists can be a threat until the fiction gives
 them names. Survivors can be talked to, traded with, betrayed, or saved.
- **Animals** are threats with instincts and territory. They usually prefer to
  flee or bluff before a fight.

## Survival: food, water, shelter, weather

- **Daily needs:** each character needs one food and one water per day. Followers
  and NPCs at the base count too. Animals need feed; vehicles need fuel.
- **Food and water** are items with uses. Water containers hold uses; a
  waterskin is not infinite. Purification (boiling, tablets, a filter) is a
  `Chemistry` or `Survival` task and may need fuel or time.
- **Spoilage:** perishable food carries a timer. Meat, produce, and opened cans
  spoil; smoked, salted, canned, and dried food lasts. A cold cellar or winter
  weather slows spoilage.
- **Shelter:** a safe rest needs a secure shelter: a base, a sealed room, an
  enclosed vehicle, or a camp with a watch and a fire. Without shelter, rest is
  interrupted: no full recovery, +1 *Exhausted*, and a night encounter or noise
  risk. A watch reduces the chance of surprise.
- **Weather:** the first winter brings cold, rain, snow, and wind. Exposure
  causes *Exhausted* or *Fever*; fuel and warm clothing matter. Heat, smoke, and
  fire are also resources and noise.
- **Foraging and hunting:** `Forage` finds food and water; `Track` finds game.
  Scarcity depends on season, region, and contamination. Do not let a good roll
  turn the apocalypse into a picnic.
- **Base stores:** the shelter has a shared inventory. Food, water, fuel, ammo,
  medicine, and parts are counted; spoilage and theft are real.

## Vehicles

A vehicle is a party asset with its own sheet, not a character.

| Field | Meaning |
|-------|---------|
| Speed | travel pace and chase edge |
| Handling | modifier to `Drive` under pressure |
| Armor | harm reduction for the vehicle |
| Condition | 0–6; at 4 rattling (−1 handling), at 6 broken |
| Fuel | 0–6; one point per travel leg or chase scene |
| Seats | how many people fit |
| Cargo | inventory slots for gear and loot |
| Noise | how loud it is (engine, horn, stereo) |

Vehicle types: bicycle (no fuel, quiet, slow), motorcycle (fast, loud, 1 seat),
sedan (4 seats), pickup (2 seats + cargo), van (6 seats + cargo), box truck
(cargo, slow), bus (12 seats, slow), armored truck (armor 2, cargo), ambulance
(4 seats, medkit), fire engine (water tank, loud), boat (water travel).

- **Travel:** a vehicle reduces travel time and carries cargo, but it spends
  fuel and makes noise. Roads, weather, and wrecks matter. A vehicle can be a
  shelter if it is enclosed and the engine is off.
- **Damage:** crashes, gunfire, hordes, and hazards damage condition. At 6 the
  vehicle is stranded until repaired. Repair uses `Mechanics`, parts, and time;
  `Vehicle Repair` handles the hard cases.
- **Chases:** an escape clock and a pursuit clock, both 0–6. The driver rolls
  `Drive`; passengers shoot, repair, spot, or navigate. At 6 on the escape
  clock the party gets away; at 6 on the pursuit clock they are caught, boxed
  in, or crash. Ramming harms both vehicles and everyone inside.
- **Noise:** an engine adds 2 to the scene's noise clock; a horn or stereo adds
  more. A vehicle is a beacon in a quiet world.
- **Ownership:** vehicles belong to the party, not a character. They can be
  stolen, abandoned, hidden, or traded. A vehicle's cargo is a separate
  inventory.

## Exploration and rare loot

### Site procedure

A searchable place is created from a **site type**: the ruleset's baseline
rooms, loot, threats, and find difficulty. The AI GM makes each one unique with
the `site-detail` and `site-secret` tables and its own telling detail. Common
sites sit anywhere; uncommon ones need a lead; hidden and secret ones must be
earned through a rumour, map, survivor, sign, radio call, or named contact.

1. **Rumour or discovery:** learn that the place exists and roughly what it is.
2. **Plan:** choose approach, gear, vehicle, and who stays home.
3. **Approach:** `Sneak`, `Drive`, `Assess`, or `Navigate`; noise starts.
4. **Explore a zone:** name the room, call for `Scavenge` or `Salvage` when it is
   risky, then call `search_site` with the total and the zone. Each search marks
   that zone picked over, raises the site's danger by 1, and returns granular
   items: two on a 10+, one on a 7–9, none on a 6–. A full pack leaves loot
   behind. When a 7–9 cost is that the loot is in poor condition, pass
   `condition worn` so the found item is worn rather than serviceable.
5. **Complication:** at site danger 3 and 5 a complication, omen, or threat
   arrives; at 6 the site turns on the party.
6. **Extract:** carry the loot out; the trip home is part of the score.

### City navigation

A city is a network of blocks, not a blank map. The AI GM builds blocks from
site types: several houses, a corner store or fast-food place, and one rarer
site worth the trip. Crossing between places is a **Navigate** move when it
matters and then `navigate_city`, which moves the party, rolls a street event,
and raises the visible **City Alert** clock by the route's cost.

| Route | City Alert | Character |
|-------|-----------|-----------|
| Main street | +2 | Fast, open, watched |
| Side street | +1 | Ordinary cover |
| Alley | 0 | Slow, cramped, dangerous |
| Rooftops | 0 | High, exposed to falls and gaps |
| Through buildings | 0, noise +1 | Slow, loud, out of sight |
| Storm drain | 0 | Dark, wet, narrow |

At City Alert 3 a patrol or runner pack is out; at 4 roads close; at 5 a horde
is drawn; at 6 the district sweeps. Every crossing is time, noise, and exposure,
and the trip home with loot is a scene.

### Maps

Hordes reads as a road atlas rather than a parchment chart. The rule set gives
the campaign a **map style**: the world map is drawn as an **atlas** chart on
the `roadmap` palette, with a block grid and no clouds or contours, and every
local map starts as a **plan** on the `concrete` palette. The GM may still
change the chart, palette, and board style in play with `update_world_map` and
`update_local_map`.

The world map is ground plus the roads across it. Ground is drawn with
`create_region` from the rule set's world terrain (city, suburbs, industrial,
ruins, farmland, forest, hills, water, highway). Roads are their own feature:
`create_road` lays a highway, street, track, or rail line over the ground,
along the same corners as the places it reaches, and `update_road` moves or
reveals one. A road appears on the player's map once it is discovered or a
discovered place stands on it, and its name sits at the middle of its length.
The atlas look means a district reads as blocks and the ways between them.

Local maps use the modern templates: `city_block`, `street_corner`,
`store_aisle`, `warehouse`, `clinic`, `lab`, `tunnel`, `storm_drain`,
`apartment_floor`, and `checkpoint`, painted with the add-on terrain (asphalt,
sidewalk, concrete, tile, rail bed, metal) and objects (cars, trucks, a
dumpster, sandbags, street lights, gurneys, barricades, generators, hatches).
Core's room, house, temple, cellar, and cave templates remain for the older
buildings the city still has. Hordes does not run encounters, so a local map is
a shared picture of where everyone stands; it never gates movement.

### Site tiers

| Tier | Examples | Danger | Loot |
|------|----------|--------|------|
| 1 | houses, apartment blocks, convenience stores, fast food | low | food, tools, clothes, batteries |
| 2 | pharmacy, school, hardware store, gas station, church | moderate | medicine, fuel, parts, ammo |
| 3 | hospital, gun shop, police station, supermarket, cult basement | high | antibiotics, firearms, armor, radios, occult relics |
| 4 | cybernetics lab, lab, university, large hospital | severe | scientific items, cybernetics, data, zombie cyborgs |
| 5 | military checkpoint, bunker, research facility, quarantine zone | extreme | unique relics, prototypes, cures |

### Rarity and condition

- Rarity: common, uncommon, rare, unique.
- Condition: broken, worn, serviceable, fine. Condition affects function,
  repair, and barter value.
- Every loot roll can add a complication: a noise, a previous scavenger, a
  locked door, a body, a trap, a camera, a survivor.

### Scientific items

Antibiotics, antivirals, insulin, morphine, adrenaline, blood bags, sterile
kits, water filters, seeds, fertilizer, batteries, solar panels, radio parts,
computers, data drives, lab glass, chemicals, experimental serums, vaccines.
Scientific loot is often the only cure, the only power, or the only clue.

### Cybernetics

| Implant | Benefit | Rejection | Notes |
|---------|---------|-----------|-------|
| Cyber-eye | +1 to aimed shots; low-light vision | 1 | needs power |
| Targeting link | +1 Firearms | 2 | military grade |
| Subdermal armor | +1 armor | 2 | obvious to scanners |
| Prosthetic arm | +1 Grit for feats of strength | 1 | maintenance |
| Prosthetic leg | +1 Athletics; ignore a leg wound | 1 | maintenance |
| Neural jack | +1 Computer Use; interface with machines | 2 | headaches |
| Adrenal pump | once per scene, +1 to a physical roll | 2 | addictive |
| Data port | store and read data; +1 to recall | 1 | visible |
| Built-in tool | a tool without hands | 1 | specific |
| Black-market heart | ignore one dying result | 3 | rare, dangerous |

Cybernetics are installed with `Cybernetics Configuration` and `Field
Surgery`. A clinic, sterile tools, and a skilled medic make it safe; a field
install does not.

### Occult relics

| Relic | Benefit | Dread | Notes |
|-------|---------|-------|-------|
| Saint's finger | once, stabilize a dying character | 1 | warm to the touch |
| Black mirror | see a place you know | 2 | something looks back |
| Grimoire | learn a rite | 2 | pages change |
| Mourning mask | speak with the dead | 2 | they remember you |
| Bone idol | +1 to one roll, once per scene | 2 | it wants offerings |
| Iron bell | drive off a horror | 2 | heard for miles |
| Salt | ward a threshold | 1 | consumed |
| Grave candle | light that the dead avoid | 1 | burns cold |
| Chained book | ask a question of something old | 3 | it asks one back |
| Reliquary | +1 armor against the eldritch | 2 | heavy, obvious |

Relics are not magic items in the D&D sense. They are bargains with something
that does not care about you. Using one is a choice with a cost.

### Knowledge loot

Manuals, maps, keys, codes, passwords, formulas, research notes, and names.
Knowledge loot unlocks recipes, knowledge moves, sites, and weaknesses. It is
often more valuable than a gun.

## Crafting, repair, and barter

### Recipes

A recipe names inputs, tools, a skill, a difficulty, a time, an output, and a
risk. The engine matches inputs, spends uses, rolls the check, and records the
output.

Examples:

- **Reload 9mm:** 1 use of reloading press, 1 box of 9mm components, 1 hour,
  `Mechanics`; failure wastes components.
- **Antiseptic:** 1 chemical, 1 clean container, 1 hour, `Chemistry`; failure
  makes a weak batch.
- **Purified water:** 1 water, fuel or tablets, 10 minutes, `Survival` or
  `Chemistry`.
- **Smoked meat:** 1 meat, salt, fuel, 4 hours, `Survival`; preserved food.
- **Ammo, improvised:** 1 parts, 1 chemical, 1 hour, `Mechanics`; unreliable
  ammunition (jams on a 7–9 or 6–).
- **Barricade:** 2 parts, 2 hours, `Mechanics`; +1 base defense.
- **Biodiesel:** 1 fuel source, 1 chemical, 4 hours, `Chemistry`; 1 fuel.
- **Field surgery kit:** 2 clean cloth, 1 antiseptic, 1 hour, `Medicine`.
- **Ward:** salt, a rite, 1 hour, `Occult Lore`; keeps a horror at bay for a
  scene.

### Repair

- Items have condition (broken, worn, serviceable, fine). Use and 6– wear them;
  repair restores one step per `Fix` result.
- Vehicles use condition 0–6 and fuel 0–6.
- Base improvements have condition and can be damaged in a raid.
- Jury-rigging works for a scene and then breaks worse.

### Barter

- There is no universal currency. Ammo, medicine, fuel, food, and parts are the
  closest things to money.
- Every trade is item-for-item. A settlement has *wants* and *has*; scarcity,
  relationship, and `Persuade`/`Trade` shift the deal.
- The engine validates that both sides hold what they offer, records the
  transfer, and refuses a trade that is wildly unfair unless the fiction
  supports it. Favours and debts are notes and relations, not balances.
- Loot condition and rarity change value. A working generator is worth more
  than a crate of broken radios.

## Base and settlement

The base is a shared location with state, improvements, stores, and people.

| Field | Meaning |
|-------|---------|
| Defense | how hard it is to overrun |
| Security | alarms, watches, locks, and patrols |
| Comfort | beds, heat, food, morale |
| Stores | shared food, water, fuel, ammo, medicine, parts |
| Population | PCs, followers, dependants |
| Condition | 0–6 integrity of the whole site |

### Improvements

Barricade, gate, watchtower, alarm, well, rain catcher, garden, smokehouse,
solar panel, generator, workshop, infirmary, radio room, armory, garage,
kennel, chapel, still, forge, cold cellar, latrine, beds.

Each improvement is a project: materials, time, a skill check, and sometimes a
specialist. It grants defense, security, comfort, storage, or a new capability
(radio, power, water, medicine, repairs). Improvements have condition and
upkeep.

### Base attacks

1. A threat or faction clock reaches the shelter.
2. The GM frames the assault with a soft move; defenders choose positions and
   moves.
3. `Defend`, `Shoot`, `Fix`, `Command`, and `Treat` resolve the fight. There is
   no initiative; the fiction and the clock drive the spotlight.
4. Damage applies to defenders, improvements, stores, and the base condition.
   A breach is a hard move, not a hit-point total.
5. After the attack: casualties, repairs, shortages, and a faction clock.

### Settlement life

Downtime at the base or a settlement uses the same moves: `Trade`, `Train`,
`Recuperate`, `Fix`, `Craft`, `Persuade`, `Command`, and `Face Horror`.
Between expeditions the GM advances faction clocks, NPC agendas, weather,
supply, and base projects. The player chooses what their character does; the
world moves without them.

## NPCs and factions doing things

The AI GM handles most of this; the engine supplies structure where the AI
would otherwise forget or cheat.

- **Named NPCs** have a goal, an instinct, a location, resources, and a
  relationship to the party. The GM records the goal with `set_npc_agenda`,
  moves them with `move_actor`, and updates what the party learns with
  `update_npc_knowledge`. Important NPCs get full sheets; minor ones are notes
  with a name and a want.
- **Off-screen faction clocks** track settlements, raider crews, cults, military
  remnants, and rival scavengers. Each front has a clock (0–6), a goal, and a
  move it makes when it fills. The GM advances fronts on time jumps and between
  expeditions, then shows the result as a rumour, a price, a patrol, a raid, or
  a changed settlement.
- **Companions and followers** are hirelings with tags, harm, morale, skills,
  and a cost. They are not turn-based combatants; the GM runs them with moves.
  A player can `Order Follower`; on a 7–9 the follower hesitates, wants
  something, or gets hurt. Followers eat, need shelter, and can leave.
- **Rival scavenger crews** are a faction plus a named leader plus a clock. They
  race the party to sites, steal loot, offer trades, and remember grudges.
- **Settlement life** is downtime: trade, politics, rumours, teaching, healing,
  repairs, and personal drama. The base and settlement sheets hold the state;
  notes and relations hold the story.
- **Engine scope:** clocks, faction turns, agendas, follower sheets, morale,
  base state, and trade validation. The AI GM does the voices, plans, and
  improvisation. Do not build a full economy or a turn-based NPC simulator.

## Advancement

- No XP and no levels.
- **Gear** is the main progression: a working rifle, a vehicle, a generator, a
  cybernetic eye, a relic, a better shelter.
- **Skills** improve through practice: 3 ticks to rank 1, 6 total to rank 2.
  Ticks come from risky use, training, teaching, and manuals.
- **Knowledge moves** are learned through teaching, books, or difficult
  experimentation. They unlock new procedures, not bigger numbers.
- **Allies, reputation, and the base** are progression too. A safe shelter, a
  doctor, a mechanic, and a radio matter more than a +1.
- **Scars** are permanent story marks from dying, infection, surgery, or
  horror. They can add a condition or a fictional limitation; they are not a
  second XP track.
- Stats do not rise. The playbook's spread is the character's shape; skill,
  gear, and knowledge are what change.

## Campaign options and tables

| Option | Values | Default |
|--------|--------|---------|
| Cause of the fall | table / unwritten | unwritten |
| Region | rural county, small city, coast, mountains, suburbs, industrial belt | rural county |
| Season | first winter, late autumn, thaw, summer | first winter |
| Horde pressure | quiet, stirring, migrating, siege | stirring |
| Scarcity | tight, standard, plentiful | standard |
| Weirdness | scientific, cybernetic, occult, all | all |
| Hope | grim, stubborn, community | stubborn |
| Starting shelter | fragile, none, strong | fragile |
| NPC activity | low, standard, high | standard |
| Zombie speed | slow, mixed, fast | mixed |
| Survival needs | on/off (engine switch) | on |

Tables:

- Setting: `apocalypse-cause`, `region`, `season`, `shelter`, `settlement`,
  `faction`, `threat`, `omen`.
- Sites: `site-type`, `city-block`, `site-detail`, `site-secret`,
  `site-occupant`, `site-sign`, `site-complication`, `site-omen`.
- Loot: `loot-scientific`, `loot-cybernetic`, `loot-occult`, `loot-knowledge`,
  `loot-condition`.
- City: `street-event`, `street-hazard`.
- Threats: `horde-behavior`, `horror-sign`, `zombie-variant`, `survivor-crew`,
  `animal`.
- People: `npc-drive`, `npc-burden`, `npc-quirk`, `name`.
- World: `weather`, `road-event`, `trade-good`, `base-trouble`, `rumour`,
  `job`, `faction-front`.
- Creative oracle: plot twist, clue, complication, opportunity.

Table genre: add `apocalypse` to `TABLE_GENRES`; Hordes uses
`["generic", "apocalypse"]` and tones `["grim", "mystery", "adventure"]`.

## Content inventory

| File | Contents |
|------|----------|
| `rule_set.json` | head, capabilities, stats preset, item sizes, skill progression, table genres/tones, recommended worlds |
| `stats.json` | Grit, Reflexes, Wits, Presence, Know-how |
| `skills.json` | the ten skills |
| `character_fields.json` | drive, burden, scar, look, background, shelter duty, personal item |
| `character_layout.json` | creation layout |
| `character_templates.json` | six standard characters |
| `playbooks/*.json` | six playbook templates |
| `moves.json` | shared moves and knowledge moves |
| `combat.json` | moves mode, harm track, armor, morale, ranges, death |
| `conditions.json` | ~20 conditions |
| `counters.json` | harm, infection, Rejection, Dread, practice ticks, follower morale |
| `items/` | weapons, ammo, armor, gear, medical, food, water, fuel, parts, granular loot |
| `site_types.json` | twenty generated place kinds with zones, loot, threats, and find levels |
| `roads.json` | highway, street, track, and rail line kinds for the world atlas |
| `encounter_content.json` | modern street and interior terrain, objects, and the city-block, tunnel, and other local map templates |
| `threats.json` | horde, zombie variants (including zombie cyborgs), horrors, survivors, animals |
| `vehicles.json` | vehicle templates |
| `recipes.json` | crafting, reloading, medicine, fuel, repairs |
| `improvements.json` | base improvements |
| `tables.json` / `creative.json` | setting, site, loot, threat, people, oracle tables |
| `worldgen_tables.json` | opening setting rolls |
| `travel.json` | walking, biking, and driving speeds for city legs, plus overland travel and weather |
| `prompts.json` | PbtA combat, harm, noise, survival, exploration, sites, city navigation, NPCs, base, etc. |
| `tool_calls.json` | Hordes tools built on generic services |
| `rest.json` | rest lengths, recovery, shelter requirement |
| `hooks.json` | Hordes policy hooks |
| `../worlds/unwritten-apocalypse/world.json` | description-only world |

Icons: use existing `skull`, `campfire`, `backpack`, `book`, `globe`, `sword`,
`shield`, `eye`, `handshake`, `skill`, `speech`, `horse`, `library`, `academy`
where they fit; add `biohazard`, `horde`, `pistol`, `rifle`, `car`, `fuel`,
`medicine`, `radio`, `relic`, `cybernetic`, `shelter`, `footprint` as needed.

## Engine additions (generic)

Every system below is generic and parameterized by content. Hordes is the first
consumer, not a special case.

| System | Generic behavior | Hordes use | Closest existing system |
|--------|------------------|------------|-------------------------|
| Campaign clocks | named 0–N segment tracks with scope, visibility, thresholds, and completion effects; `start_clock`, `advance_clock`, `end_clock`, `list_clocks` | noise, horde pressure, faction fronts, chases, base defense, site danger | projects/faction turns/counters; no campaign clock |
| Counter thresholds | `thresholds` on counters, firing effects at chosen values | harm 4/6, infection 3/4, Rejection 3/5/6, Dread 3/5/6, follower morale | only `full` at max |
| Harm track | a reusable 0–max harm model with armor, piercing, thresholds, dying, recovery; `apply_harm` to characters, NPCs, and threats | player harm, threat capacity, hordes, vehicles | `harm_character`, `DyingRules`, conditions |
| PbtA combat mode | `combat.mode: "moves"` hides encounters, initiative, turns, attack budgets, and grid gates; keeps moves, conditions, counters, harm | the whole combat system | `combat` bool + encounter tools |
| Move targeting and effects | `target` on moves and `roll_move`; effect ops `harm`, `ammo`, `noise`, `clock`, `condition`, `item`, `vehicle`, `practice`; engine validates targets and applies | shoot/engage/defend, horde attacks, ammo, noise | move effects (note, counter, condition, save) |
| Skill practice | per-character practice ticks per skill; `roll_move` ticks the move's (or chosen) skill once per scene; thresholds raise rank; `train_skill` | ten skills, rank 0–2 | skills + level-up picks |
| Move skill choice | a move may allow a skill from a list chosen at roll time, such as Defy Danger; the engine adds and marks the chosen skill | Defy Danger and improvised approaches | `stat_choice` for stats; no skill choice |
| Learned moves | `MoveRules.requires_feature`/`learned`; `learn_move`, `teach`, books, experimentation; engine refuses an unlearned move | knowledge moves | all moves shared; features |
| Firearms and ammo | item ammo metadata (caliber, capacity, reload), carried loaded count, `reload`, shot consumption, jams, noise, piercing | guns, ammo scarcity, reloads | item uses; no loaded state |
| Item condition | condition states on carried items (broken/worn/serviceable/fine; vehicles 0–6), wear on use/6–, `repair_item`, breakage | weapons, tools, vehicles, base improvements | item uses only |
| Threat actors | `ThreatTemplate`/`Threat` with harm, armor, tags, moves, magnitude/clock, location, visibility; create/harm/advance/split/merge/remove/list; no turns or attack rolls | hordes, zombies, horrors, hostile bands, animals | NPCs + `large_group` + monsters |
| Vehicles | `VehicleTemplate`/`Vehicle` party asset with seats, cargo, condition, fuel, armor, speed, noise; travel, chase clocks, repair | cars, trucks, boats | travel modes/items; no state |
| Recipes and repair | `recipe` content with inputs, tools, skill, time, output, risk; `salvage`, `craft`, `repair`; ingredient matching | ammo, medicine, food, fuel, base, vehicles | projects + checks |
| Barter | `trade_value` on items; settlement wants/offers; `trade` validates item-for-item deals and value tolerance; no universal currency | scavenging economy | marketplace/currency |
| Site improvements | `improvement` content plus site state; projects, defense, comfort, security, stores, upkeep, attack clocks | the base | projects/locations/factions |
| NPC agendas and fronts | agenda/goal fields on NPCs; `set_npc_agenda`; faction fronts over clocks; off-screen advancement on time jumps | named NPCs, rival crews, settlements | NPC summary/knowledge, factions, projects |
| Table genre | add `apocalypse` to `TABLE_GENRES` | all Hordes tables | closed genre tuple |
| UI and prompts | clock panel, harm track, ammo/condition badges, vehicle sheet, base panel, single threats as statblocks, a scale threat's line under the map, no encounter panel; PbtA and survival prompts | the whole game | existing campaign UI |

### Implementation notes

- **Clocks are the backbone.** Noise, horde pressure, faction fronts, chases,
  base defense, and site danger are all the same object with different names,
  scopes, thresholds, and visibility. Do not build six bespoke counters.
- **Harm is engine state, not narration.** A move band's `harm` effect and the
  `harm_character`/`harm_threat` tools all call one `apply_harm` path. Armor,
  piercing, thresholds, dying, and death are enforced there.
- **No turn-based leftovers.** In moves mode the engine must not require an
  encounter, initiative, a turn, or a grid cell for an attack, a defense, or
  harm. A player can shoot on any beat the fiction gives them.
- **Targets are explicit.** A move that harms a target takes a `target`
  argument. The engine validates that the target is present and applies the
  band's effect; it does not let the GM invent a hit.
- **Practice is automatic.** `roll_move` ticks the named skill once per scene.
  The GM does not have to remember; the sheet shows ticks.
- **Knowledge moves are gated.** `roll_move` refuses a knowledge move the
  character has not learned, with a message naming the learning paths.
- **Ammo and condition are item state.** A firearm's loaded count and an item's
  condition live on the carried item, survive export/import, and show on the
  sheet.
- **Threats are not NPCs.** They have no character sheet, no turn, and no
  attack roll. Named people stay NPCs.
- **Vehicles are assets.** They are not characters; they do not take turns.
- **Recipes and trade are validated.** The engine refuses missing inputs,
  missing tools, and trades that are not backed by held items.
- **Backwards compatibility.** New definition types and optional fields default
  off; existing rule sets keep their behavior. No campaign major-version bump
  until a shipped ruleset uses a new required field.

## Ruleset-specific code

Keep `rules/hordes.py` thin. It holds only the policy that cannot be expressed
as data:

- `new_day`: weather, food/water, spoilage, infection progression, Rejection
  maintenance, Dread recovery, faction clocks.
- `before_rest` / `after_rest`: shelter check, recovery, interruptions.
- `after_consume`: food, water, medicine, stimulants, contaminated water.
- `stabilize`: field surgery and the death clock.
- `held_back`: followers, dependants, and base upkeep.
- Horde split/merge tables and vehicle fuel/condition rates are data
  (`ThreatTemplate.split`, `VehicleTemplate.fuel_use`), not Python branches.
- Tool bindings in `tool_calls.json` call generic services:
  `create_threat`, `harm_threat`, `threat_move`, `split_horde`, `merge_horde`,
  `start_clock`, `advance_clock`, `end_clock`, `list_clocks`, `advance_noise`,
  `create_vehicle`, `damage_vehicle`, `refuel`, `repair_vehicle`, `reload`,
  `craft`, `salvage`, `repair_item`, `trade`, `learn_move`, `train_skill`,
  `set_npc_agenda`, `order_follower`, `improve_site`, `damage_base`,
  `repair_base`, `install_cybernetic`, `treat_rejection`, `use_relic`,
  `treat_dread`, `roll_loot`.

## Verification

- Content loader tests: every new definition type, cross-references (ammo
  calibers, recipes, threats, vehicles, improvements), genres, icons, and the
  world.
- Character tests: six playbooks, stat spreads, trained skills, knowledge
  moves, kits, standard characters, practice ticks, rank-ups.
- Move tests: every band, `roll_move` math, target validation, harm/ammo/noise
  effects, learned-move refusal, practice marking.
- Harm tests: armor, piercing, thresholds, wounded, dying, death clock,
  stabilization, recovery, conditions.
- PbtA conformance test: a full fight produces no encounter, initiative, turn,
  or attack-budget events, and no threat rolls to hit.
- Horde tests: magnitude, split, merge, noise attraction, horde attack, break.
- Survival tests: food/water, spoilage, shelter, weather, infection,
  Rejection, Dread, medicine.
- Vehicle tests: fuel, condition, travel, chase clocks, repair.
- Crafting/trade tests: recipes, salvage, repair, barter validation.
- Base tests: improvements, projects, defense, upkeep, attack clocks.
- NPC/faction tests: agenda, followers, faction turns, off-screen clocks.
- Live DeepSeek runs: opening, a three-zombie fight, a horde+noise scene, a
  scavenging run, a vehicle chase, a base raid, a cybernetic install, a relic
  use, a barter/crafting scene, and a long-play session. Verify the narration
  never uses initiative/turns/HP and that the engine records the crunch.
- Full suite, `cygnusloom content validate` with zero warnings, and
  `ruff check`.

## Phases and todo list

### Phase 0 — ruleset skeleton and PbtA core
- [ ] `rule_set.json`, `stats.json`, `skills.json`, fields, layout, six
      playbooks, six standard characters.
- [ ] Shared moves and knowledge moves.
- [ ] `combat.mode: "moves"`, harm track, conditions, dying.
- [ ] Generic move targeting/effects, harm application, practice ticks.
- [ ] Setting tables, world `unwritten-apocalypse`, opening prompts.
- [ ] Compendium entries, icons, creation/sheet UI.
- [ ] Live fight proves no encounter/initiative/turns.

### Phase 1 — survival crunch
- [ ] Campaign clocks (noise, horde pressure, fronts, chases, site danger).
- [ ] Threat actors, horde magnitude, split/merge, threat moves.
- [ ] Ammo, reloads, jams, noise, firearm ranges.
- [ ] Food/water/shelter/weather/spoilage.
- [ ] Infection, medicine, Rejection, Dread.
- [ ] Skill practice and learned knowledge moves.

### Phase 2 — vehicles, crafting, trade, base
- [ ] Vehicles, fuel/condition, travel, chase clocks, repairs.
- [ ] Recipes, salvage, repair, barter.
- [ ] Base improvements, stores, defense, upkeep, raids.
- [ ] NPC agendas, followers, faction clocks, settlement downtime.
- [ ] Loot tables, scientific/cybernetic/occult loot, knowledge loot.

### Phase 3 — balance and release
- [ ] Long-play live runs; tune harm, noise, scarcity, and horde pressure.
- [ ] Icon and compendium audit; 390px layout; accessibility.
- [ ] Credits and legal notices; confirm the name.
- [ ] Full suite, content validation, ruff, docs.
- [ ] Update `ISSUE_TRACKER.md` (the zombie item stays until implementation
      is complete).

## Deferred

- Mutation system, full settlement economy, PvP base raiding, grid-based
  vehicle combat, procedural maps, deep crafting trees, and a full NPC
  simulator.
- Cataclysm-derived content (requires a CC BY-SA 3.0 notice and share-alike).
- Multiplayer spotlight arbitration beyond the GM's judgment.
- Weapon-by-weapon durability and caliber-by-caliber reloading beyond the
  bundles above.
- A separate "hope" clock; hope is a campaign option and fiction.
