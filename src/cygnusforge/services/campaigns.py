from __future__ import annotations

import json
import random
import re
import threading
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.content.character import CampaignSpec, CharacterSpec, roll_stats
from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import (
    ContentLibrary,
    RuleSet,
    ToolCall,
    WorldContent,
    WorldTemplate,
)
from cygnusforge.db.models import Campaign, Event
from cygnusforge.llm.base import (
    ChatReply,
    LLMError,
    Provider,
    TextDelta,
    ToolRequest,
    complete,
)
from cygnusforge.llm.registry import ProviderFactory
from cygnusforge.rules.context import ToolContext, brief
from cygnusforge.rules.dice import roll_total
from cygnusforge.services import combat, encounters
from cygnusforge.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusforge.services.events import (
    CHARS_PER_TOKEN,
    CampaignState,
    Channel,
    ChatGM,
    ChatPlayer,
    ClockAdvanced,
    ContextPart,
    GameEvent,
    JournalStarted,
    OocStarted,
    PromptShown,
    SpeechWritten,
    StateStepMade,
    SummaryUpdated,
    ToolRan,
    Turn,
    TurnBegun,
    TurnFailed,
    decode,
    estimate_tokens,
    events_since,
    is_hidden,
    last_event_id,
    load_state,
    name_key,
    record,
    speaker_name,
    truncate,
)
from cygnusforge.services.names import next_campaign_label
from cygnusforge.services.tools import (
    Ask,
    RollAnswer,
    TextAnswer,
    ToolRun,
    run_tool,
    tool_specs,
)
from cygnusforge.services.world import (
    WorldTools,
    create_player_character,
    present_rules_text,
    promote_hireling,
    seed_campaign,
    set_campaign_options,
    world_state_text,
)

MAX_NAME = 120
MAX_MESSAGE = 8000
SYSTEM_PROMPT = (
    "You are Cygnus, an AI game master (GM). You run the world of a roleplaying campaign: you "
    "describe the scene, voice its characters, decide the outcome of the player's actions, and keep "
    "the story consistent with what has already happened. You are not a general assistant; stay in "
    "the GM role for the whole campaign. You never speak, think, feel, decide, or act for the "
    "player character: the player alone chooses what their character says and does, and you "
    "describe only what happens as a result. A hireling the player controls is different: the "
    "player chooses its actions, and you voice its dialogue through speak."
)
# Spoken words never contain angle brackets or call syntax, so either means a tool call was written.
SPOKEN_MARKUP = re.compile(r"<[^>]*>|\b\w+\(\s*\{")
# An empty speech reply is retried this many times before the step gives up.
SPEECH_ATTEMPTS = 3
# An empty narration is requested again this many times before the turn fails.
NARRATION_ATTEMPTS = 2
JOURNAL_ROUNDS = 3
STATE_INSTRUCTION = (
    "State management step. If the player is speaking out of character (to you as the GM rather "
    "than to their character, about the rules or the world, or asking for a change outside the "
    "story), call ooc and make no other tool calls: the out-of-character step that follows replies "
    "and makes any change. Otherwise, look up what the action depends on with the lookup tools, "
    "decide what this action changes and what needs to be said or done, then make it so with tool "
    "calls: create, update, or delete locations, characters, and "
    "notes as the story needs; move characters as the story needs. The player character's "
    "recorded location must match the narration: when this action or its events take them to another "
    "place, such as a room, building, or area, discover that place first if the story shows it "
    "(discover_location), then move them there (move_actor) in this step, before the narration "
    "describes them arriving. Entering a place is always a move_actor call; update_location only "
    "changes a description and never moves anyone. Never narrate them in a place the state does not "
    "record; ask for a roll "
    "when the outcome is uncertain; and call "
    "speak once for each NPC who says something, with everything they say in one intent, without "
    "writing their words yourself. An NPC who is not at the player character's location gets a warning "
    "and no dialogue; pass force true only when they really are speaking from elsewhere. "
    "Use place_object to stock a place with treasure or an item only when the story or the rules "
    "call for it; never invent treasure unprompted, and never place again an object the place already "
    "holds. Characters use take_object to pick up an object "
    "in their place, naming it by the short name given to place_object. In an "
    "encounter, keep each turn's narration to a few sentences, and never put an NPC's words in quotes "
    "inside the narration; those go through speak. "
    "When a scene or encounter ends, delete the NPCs who only passed through "
    "it, such as strangers, guards, and monsters, and who matter little to the story; keep anyone "
    "the story may bring back. An NPC who fled an encounter and matters to the story does not stay "
    "where the fight was: move it to the place it fled to, which may be a different location, or, if "
    "it simply vanished, move it into the wind with move_actor and no location. An NPC in the wind "
    "is at no location; bring it back with move_actor when the story needs it. An NPC who falls in "
    "combat stays at its location as a body, marked defeated in all_at_location; delete it "
    "once it no longer matters to the story. When a fall or surrender changes what a location "
    "looks like, update that location's description so it no longer shows the NPC alive. "
    "An encounter ends on its own when one side can no "
    "longer fight. Notes and narration must agree with the recorded harm and status: never "
    "call a character unhurt or scratched while they carry harm, and never describe a defeated or dead "
    "NPC as alive, conscious, breathing, or moving. Notes hold settled facts that the story could lose, "
    "such as secrets, promises, debts, and clues. Do not copy into a note anything a location, character, "
    "statblock, or NPC record already holds. Before adding a note, check the existing notes: update one "
    "rather than repeating a fact. Record only settled facts, not actions still in progress. Do not "
    "narrate here; the narration comes in the next step."
)
# Closes the state step after the ruleset's fragments.
STATE_CLOSING = "When nothing else needs to change, reply with one short sentence saying so."
NARRATE_INSTRUCTION = (
    "Narration step. Write the next beat as a good editor would: specific, "
    "in character, and free of filler. Keep it minimal: a routine beat — including arriving at or "
    "discovering a place, a short walk, an uneventful look around, or a brief exchange of dialogue — "
    "takes two or three sentences (under 60 words). When a place or NPC first comes into view, give only "
    "its immediate impression in a clause or sentence rather than cataloging every detail of its "
    "description, and frame queued dialogue with no extra buildup. Give a longer paragraph (still under "
    "100 words) only for a major beat such as a multi-action combat round, a character's death, or a "
    "multi-watch journey, covering each consequential entry of the turn log above, in numbered order, "
    "with its outcome stated plainly: each arrival and departure, encounter start, initiative, NPC action, attack and its "
    "result, save and roll outcome, surrender, flight, death, end of an encounter, and change to the player "
    "character. Several entries may share one sentence, and a later entry never happens before an earlier "
    "one. Leave out entries that change nothing the player perceives or needs, and cut repetition and "
    "restated detail. Include only the detail the player needs to act on or would notice. The turn log and "
    "the tool results above are the record of what happened. Anything they do "
    "not record did not happen: do not narrate an item as taken, placed, used, or moved, or a "
    "character or object as somewhere, unless they record it, invent no times, distances, figures, directions, "
    "or motives the log and results do not give. Invent no objects, exits, places, or people the record does "
    "not give (a ledger in a back room, a second door), and nothing that contradicts the record. Brief flavor "
    "such as a gesture, a mood, or a step within a fight is fine only when it matters and fits the placements "
    "and results. "
    "Never write a number of minutes, hours, days, or distance "
    "that the log and results do not state: for travel or waiting they do not give, say a vague span such as "
    "after a while or on the walk over. Name no street, square, alley, or route that the log and results do not "
    "name; say only the place the log gives. Do not treat the GM's earlier notes "
    "or drafts as settled. Earlier narration in this conversation is no record either: where it "
    "disagrees with the world state above or the turn log, follow those. Base the outcome on the state changes and tool results above. Describe an item as used, "
    "burned, or spent only when a tool result above records that use; otherwise the item is unchanged. "
    "Narrate what the player "
    "can perceive or is affected by: arrivals and departures, new or changed places, NPC speech, roll "
    "outcomes, the start of an encounter, who joins it, initiative and who acts first, and where the "
    "combatants stand relative to each other, consistent with the placements the results give. A failed initiative roll is news for the player: say the "
    "player character was slow to act. "
    "The transcript hides the records of NPCs, places, and objects, so the narration is how the "
    "player learns of one: narrate each the first time it comes into view. An NPC or creature is "
    "where the world state above places it: one at another location is not in the room, so do not "
    "show it acting or standing there, and if its words are queued, narrate them as coming from "
    "elsewhere. "
    "The queued dialogue is "
    "JSON: work each entry's dialogue value into the narration where it fits, copied exactly, and "
    "do not write any other words for an NPC. Weave dialogue into the prose as plain sentences, "
    "with ordinary quotation marks at most; never put it in block quotes, bullet points, labels, or "
    "other special formatting. If an entry's dialogue includes a stage direction, such as a gesture "
    "or what the speaker does, narrate that action once as prose and do not repeat it. Do not "
    "narrate notes or other GM-only records, and do "
    "not mention tool names or ids. Do not tell the player about a refused or failed tool call, a "
    "naming or bookkeeping rule, or the text of a rule or tool description; speak only in the world's "
    "terms, and if a name was changed, give the player the name as it now stands. "
    "Describe the player character's words and deliberate choices only as the player's message "
    "states them, and add none for them, not even an action such as setting an item down. The player "
    "character stays where the log and results put them: narrate them walking, crossing a street, or "
    "entering a place only when a move_actor result records it, even if the player's message asks for "
    "it or the speech suggests it. If the player's "
    "message states an action that the log does not resolve, say only what the log shows happening, and "
    "do not guess how it turned out. "
    "The first time the player character is mentioned in a "
    "narration, use their full name, since a pronoun or short name alone could be ambiguous. "
    "When the story moves them or something happens to them, "
    "narrate it as a result. Narrate everything that happens to the player character, once, in "
    "plain prose: harm, healing, {penalty_clause}injuries, conditions, items gained or lost, the effect of "
    "any magic or item use, a save's result, and any rest they take. Narrate these even when the "
    "state step already recorded them, and give any sum of money or count of items exactly as the "
    "tool results recorded it. End the beat where the player's next choice begins. When a player character dies, narrate the death in full, with what it looked like and where the body lies, then stop: the player chooses the next character, so do not choose one for them. After a recovery, "
    "narrate only how the character comes round and what it cost, then stop: do not have the player "
    "character act, and do not bring back a monster or NPC that the state places elsewhere. "
    "Do not call tools."
)
OPENING_PROMPT = (
    "Opening instruction. This is setup, not play: do not narrate a scene, speak as a character, "
    "or act for the player character. Make any tool calls first and write no text before them: "
    "place the player character in the world with move_actor, look up the starting location with "
    "all_at_location, and make any opening reading or roll the rule set calls for. Then call ooc with no_narration true. In your reply, write one sentence "
    "for each of these, in this order, and nothing more: introducing yourself as Cygnus, the game "
    "master; one for each important detail the player must know before they act; one for each player "
    "character; setting the scene; and prompting the player to begin acting. You do not need to rehash game rules or character attributes - assume the player already knows these. This is a thematic introduction.Bold any keywords or core concepts. Use line breaks to separate each sentence. Then stop and leave the "
    "player to act."
)


def opening_prompt(template: WorldTemplate | None) -> str:
    """The opening instruction, with the world's start when it has one: where the player character
    begins, and the hooks that bring the party there."""
    if template is None or template.start is None:
        return OPENING_PROMPT
    start = template.start
    place = next(loc for loc in template.locations if loc.id == start.location)
    return (
        f"{OPENING_PROMPT}\n\nThe campaign starts at {place.name} (location id {place.id}). Place the "
        f"player character there with move_actor, and do not place them anywhere else. The adventure's "
        f"hooks, which bring the party to this start, are below. Work them into the introduction as "
        f"what the player character has heard or been asked to do; do not state them as a list.\n\n"
        f"{start.hooks}"
    )


OOC_INSTRUCTION = (
    "Out-of-character step. The player is talking to you as the GM, not to their character. Reply "
    "to them directly, in your own voice, and do not narrate the scene. Answer the question in the first "
    "sentence, with no preamble or restating it; one to three sentences is enough unless they asked for "
    "more. Do not speak or act for the player character. If their request needs a change to "
    "the world, make it with tool calls first, and "
    "write no text before those calls: text written before a tool call is shown to the player "
    "right away. If the story should move on after your reply, call ooc with no_narration false."
)
_NUMBER_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


JOURNAL_INSTRUCTION = (
    "Journal step. Nothing has been narrated yet: the turn above is what happened, and the narration "
    "will be written from it after this step. The log below it is everything recorded since the "
    "journal was last written, and it may span several campaign days, since a day has ended. The "
    "journal is one text file of the campaign so far, the recap the story keeps. Keep one passage per "
    "campaign day, beginning with 'Day N:', and write one for each day the log covers. Merge a day's "
    "passage into the existing one with replace_in_journal, quoting the exact text, or start it with "
    "append_journal if the day has none yet. If the log changes or contradicts an earlier passage, "
    "correct that passage the same way. Delete passages that no longer belong. When the journal runs "
    "long, condense older days into shorter passages. Use view_journal for line numbers and "
    "search_journal to find text. If this turn is the campaign's opening, record the introduction's "
    "hooks, starting situation and player characters as the first passage. If the log holds nothing "
    "worth keeping, call no tools and write no text."
)


def narrate_instruction(rule_set: RuleSet) -> str:
    """The narration step's prompt, naming the ruleset's penalty item among what to narrate."""
    penalty = f"{rule_set.inventory.penalty_name}, " if rule_set.inventory else ""
    return NARRATE_INSTRUCTION.format(penalty_clause=penalty)


def state_instruction(rule_set: RuleSet) -> str:
    """The state step's prompt: the generic loop text, then the ruleset's prompt fragments in order."""
    return " ".join(
        [
            STATE_INSTRUCTION,
            *(fragment.text for fragment in rule_set.prompt_fragments),
            STATE_CLOSING,
        ]
    )


def speech_instruction(sentence_cap: int | None) -> str:
    """The speech step's prompt. The sentence cap comes from the ruleset's narration settings."""
    if sentence_cap is None:
        length = "one sentence where you can"
    elif sentence_cap == 1:
        length = "one sentence"
    else:
        words = (
            _NUMBER_WORDS[sentence_cap - 1]
            if sentence_cap <= len(_NUMBER_WORDS)
            else str(sentence_cap)
        )
        length = f"one sentence where you can and {words} at most"
    return (
        "Speech step. Write only the words the speaker says aloud to the player, in character, "
        "following the speaker's personality, accent, and quirks, and the intent given. Write exactly "
        f"one line for the intent, as the speaker would actually say it, {length}. Do not answer for "
        "anyone else, do not add a second line, and do not repeat a line "
        "the intent says is already queued. "
        "Do not describe actions, do not narrate, and do not add anything the intent does not call for. "
        "Stage directions such as gestures, tone, or what the speaker does belong to the narration, not "
        "to the speech: every word you write must be something the speaker says. "
        "Reply with the spoken words only."
    )


NAME_CHECK_PROMPT = (
    "You check whether a new place name repeats a place that already exists in a tabletop "
    "campaign. A new name repeats an existing place when it names the same place in other words, "
    "such as 'Farm Road' for 'Farming Path', or 'The Old Mill' for 'Old Mill'. A different place "
    "with a related name is not a repeat when it is a separate place. Reply with exactly one line: "
    "'REPEAT: <existing name>' when the new name repeats an existing place, or 'NEW' otherwise. "
    "Reply with nothing else."
)
SUMMARY_PROMPT = (
    "You are Cygnus, the AI game master of a roleplaying campaign, and you keep its memory. Given "
    "the summary so far and new events, write an updated summary. Keep names, places, facts the "
    "players established, open threads, "
    "and anything the players asked to remember. Reply with the summary only."
)
# Once the prompt reaches this share of the context window, older history is summarized.
COMPRESS_AT = 0.5
# After a summary, the newest messages worth up to this share of the window stay verbatim.
KEEP_RECENT = 0.1
# A summary may take up to this share of the window. Longer ones keep only their newest text.
SUMMARY_SHARE = 0.25
WORDS_PER_TOKEN = 0.75
SUMMARY_HEADING = "Summary so far:"


def create_campaign(
    engine: Engine,
    library: ContentLibrary,
    allowed_models: Iterable[str],
    owner_id: int,
    name: str,
    world_template: str,
    rule_set: str,
    model: str,
    rng: random.Random | None = None,
) -> Campaign:
    name = (name or "").strip()
    if len(name) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    if world_template not in library.world_templates:
        raise InvalidInput("that world template is not available")
    if rule_set not in library.rule_sets:
        raise InvalidInput("that rule set is not available")
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    template = library.world_templates[world_template]
    if template.rule_set is not None and rule_set != template.rule_set:
        raise InvalidInput(
            f"that world only works with the {library.rule_sets[template.rule_set].name} rule set"
        )
    try:
        content = library.world_content(world_template, rule_set)
    except ContentError as error:
        raise InvalidInput(f"that world does not work with that rule set: {error}") from error
    with session_scope(engine) as db:
        if not name:
            # A blank label gets the world's next default: "Twin Lakes #1", "Twin Lakes #2"...
            existing = db.exec(
                select(Campaign.name).where(
                    Campaign.owner_id == owner_id,
                    Campaign.world_template == world_template,
                )
            ).all()
            name = next_campaign_label(template.name, existing)
        campaign = Campaign(
            owner_id=owner_id,
            name=name,
            world_template=world_template,
            rule_set=rule_set,
            model=model,
        )
        db.add(campaign)
        db.flush()
        # Seeding commits the campaign's world with it. If it fails, nothing is kept.
        seed_campaign(
            db,
            campaign.id,
            template,
            library.core.tables,
            rng if rng is not None else random.Random(),
            library.rule_sets[rule_set].character,
            monsters={**library.rule_sets[rule_set].monsters, **content.monsters},
            plus=library.rule_sets[rule_set].plus,
            npc_hp=library.rule_sets[rule_set].npc_hp,
            npc_hp_stat=library.rule_sets[rule_set].npc_hp_stat,
            wilderness=library.rule_sets[rule_set].wilderness,
        )
        db.refresh(campaign)
        return campaign


def roll_player_stats(
    engine: Engine,
    library: ContentLibrary,
    owner_id: int,
    campaign_id: int,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Roll the stats a new player character starts from. The page keeps the roll until the
    player submits; the roll is not stored in the campaign."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    spec = character_spec(library, campaign)
    return roll_stats(
        spec,
        rng if rng is not None else random.Random(),
        plus=library.rule_sets[campaign.rule_set].plus,
    )


@dataclass(frozen=True)
class NewCharacter:
    """The player character a creation form stands for, before its stats are resolved."""

    name: str
    rolled: Mapping[str, int]
    submitted: Mapping[str, Any]
    pronouns: str


def save_creation(
    engine: Engine,
    library: ContentLibrary,
    owner_id: int,
    campaign_id: int,
    *,
    options: Mapping[str, Any] | None = None,
    character: NewCharacter | None = None,
) -> None:
    """Save the campaign options and the player character together, so either both are saved or
    neither is. A part left as None is not saved. The character needs the options first."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    spec = campaign_spec(library, campaign)
    with _exclusive(campaign_id), session_scope(engine) as db:
        if options is not None:
            set_campaign_options(db, campaign_id, spec, options)
        if character is not None:
            rule_set = library.rule_sets[campaign.rule_set]
            create_player_character(
                db,
                campaign_id,
                spec,
                character_spec(library, campaign),
                character.name,
                character.rolled,
                character.submitted,
                random.Random(),
                inventory=rule_set.inventory,
                item_sizes=rule_set.item_sizes,
                magic=rule_set.magic,
                currency=rule_set.marketplace.currency if rule_set.marketplace else "",
                armor_max=rule_set.combat.armor_max if rule_set.combat else 0,
                defense_base=rule_set.combat.defense_base if rule_set.combat else 0,
                defense_modifier=rule_set.combat.defense_modifier if rule_set.combat else "",
                pronouns=character.pronouns,
                plus=rule_set.plus,
            )
        db.commit()


def promote_hireling_to_character(
    engine: Engine, owner_id: int, campaign_id: int, name: str
) -> dict[str, Any]:
    """After the player character died, a living hireling takes over as the player character."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return promote_hireling(db, campaign_id, name)


def character_spec(library: ContentLibrary, campaign: Campaign) -> CharacterSpec:
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    return rule_set.character


def campaign_spec(library: ContentLibrary, campaign: Campaign) -> CampaignSpec:
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    return rule_set.campaign


def list_campaigns(engine: Engine, owner_id: int) -> list[Campaign]:
    with session_scope(engine) as db:
        query = (
            select(Campaign)
            .where(Campaign.owner_id == owner_id)
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        )
        return list(db.exec(query).all())


def get_campaign(engine: Engine, owner_id: int, campaign_id: int) -> Campaign:
    """Other accounts' campaigns look the same as missing ones, so nothing leaks."""
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        return campaign


def default_campaign_labels(
    engine: Engine, library: ContentLibrary, owner_id: int
) -> dict[str, str]:
    """The label a new campaign of each world would get by default."""
    with session_scope(engine) as db:
        rows = db.exec(
            select(Campaign.world_template, Campaign.name).where(Campaign.owner_id == owner_id)
        ).all()
    existing: dict[str, list[str]] = defaultdict(list)
    for world_template, name in rows:
        existing[world_template].append(name)
    return {
        world_id: next_campaign_label(template.name, existing.get(world_id, ()))
        for world_id, template in library.world_templates.items()
    }


# Campaigns with a turn, answer, rollback, or model change in progress. Only one of those may run
# per campaign at a time, so a second request is refused with Conflict instead of interleaving
# its writes. The set is guarded by a lock only for the moment it is read or changed, so a turn
# can span many awaits without holding the lock. This covers one server process.
_busy_lock = threading.Lock()
_busy_campaigns: set[int] = set()


@contextmanager
def _exclusive(campaign_id: int) -> Iterator[None]:
    with _busy_lock:
        if campaign_id in _busy_campaigns:
            raise Conflict("the GM is still working on this campaign; try again when it finishes")
        _busy_campaigns.add(campaign_id)
    try:
        yield
    finally:
        with _busy_lock:
            _busy_campaigns.discard(campaign_id)


def delete_campaign(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Delete a campaign and its log. Another account's campaign looks the same as a missing
    one. Refused while the GM is still writing a reply."""
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        db.execute(Event.__table__.delete().where(Event.campaign_id == campaign_id))
        db.delete(campaign)
        db.commit()


def set_campaign_model(
    engine: Engine,
    allowed_models: Iterable[str],
    owner_id: int,
    campaign_id: int,
    model: str,
) -> Campaign:
    """Switch the model every later reply in the campaign uses, including a reply that is
    paused waiting for the player."""
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = model
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


def set_campaign_label(engine: Engine, owner_id: int, campaign_id: int, label: str) -> Campaign:
    """Change the label the campaign list shows. The label is only the player's name for the
    campaign; it is not part of the log."""
    label = (label or "").strip()
    if not label or len(label) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.name = label
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


@dataclass(frozen=True)
class CampaignWorld:
    """The folded campaign state."""

    state: CampaignState


def campaign_world(engine: Engine, owner_id: int, campaign_id: int) -> CampaignWorld:
    get_campaign(engine, owner_id, campaign_id)
    return CampaignWorld(state=_state(engine, campaign_id))


def set_debug_invincibility(
    engine: Engine, owner_id: int, campaign_id: int, name: str, active: bool
) -> dict[str, Any]:
    """Turn the debug invincibility buff on or off for a player character. Refused while the GM
    is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.set_invincibility(db, campaign_id, name, active)


def set_debug_stat(
    engine: Engine, owner_id: int, campaign_id: int, name: str, stat: str, remaining: int
) -> dict[str, Any]:
    """Set a player character's current score for one stat from the debug panel. Refused while the
    GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.set_debug_stat(db, campaign_id, name, stat, remaining)


def set_debug_currency(
    engine: Engine, owner_id: int, campaign_id: int, name: str, feature: str, amount: int
) -> dict[str, Any]:
    """Give a player character some of the rule set's currency from the debug panel. Refused while
    the GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.add_debug_currency(db, campaign_id, name, feature, amount)


@dataclass(frozen=True)
class ChatMessage:
    """A line of the chat: the player's, or the GM's reply with the model that wrote it. `id` is
    the id of the event that recorded it. `speaker` is the character a player's message spoke for."""

    id: int
    role: str
    content: str
    model: str | None = None
    speaker: str | None = None
    reasoning: str = ""


def list_messages(engine: Engine, campaign_id: int) -> list[ChatMessage]:
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        return [_chat_message(row) for row in rows]


def campaign_transcript(
    engine: Engine, owner_id: int, campaign_id: int
) -> list[ChatMessage | LogLine]:
    """The chat as shown: the messages with the Log's entries among them, in the order they
    happened."""
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        # A failure is news only until a later step goes through, as in load_state.
        progress = max(
            (row.id or 0 for row in events_since(db, campaign_id) if row.kind != TurnFailed.kind),
            default=0,
        )
        items: list[ChatMessage | LogLine] = [
            _chat_message(row) for row in events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        ]
        for row in events_since(db, campaign_id, channel=Channel.LOG):
            event = decode(row)
            if isinstance(event, TurnFailed) and (row.id or 0) < progress:
                continue
            if (text := event.render()) is not None:
                items.append(LogLine(id=row.id or 0, content=text, hidden=is_hidden(row.kind)))
    return sorted(items, key=lambda item: item.id)


def _chat_message(row: Event) -> ChatMessage:
    event = decode(row)
    if isinstance(event, ChatPlayer):
        return ChatMessage(
            id=row.id or 0, role="user", content=event.content, speaker=event.speaker
        )
    if isinstance(event, ChatGM):
        return ChatMessage(
            id=row.id or 0,
            role="assistant",
            content=event.content,
            model=event.model,
            reasoning=event.reasoning,
        )
    raise TypeError(f"{row.kind} is not a chat event")


def system_parts(
    rule_set_id: str,
    rules: Mapping[str, str],
    important: frozenset[str],
    summary: str,
    summary_through: int,
    world_text: str = "",
    present_text: str = "",
) -> list[ContextPart]:
    """The system prompt in sections. Joined with blank lines, they are the system message.

    Every rule is listed with a brief. The important rules are given in full. The others are read
    with lookup_rule.
    """
    parts = [ContextPart("system", "Base prompt", SYSTEM_PROMPT)]
    if rules:
        intro = (
            "Rules for this campaign. The important rules are given in full; "
            "read any other rule with lookup_rule."
        )
        lines = [intro]
        for rule_id, text in rules.items():
            if rule_id in important:
                lines.append(f"- {rule_id}: {text}")
            else:
                lines.append(f"- {rule_id}: {brief(text)}")
        parts.append(
            ContextPart(
                "system",
                f"Rules from rule set '{rule_set_id}': {', '.join(rules)}",
                "\n".join(lines),
            )
        )
    if world_text:
        parts.append(ContextPart("system", "World state", world_text))
    if present_text:
        parts.append(ContextPart("system", "Rules for what is present", present_text))
    if summary:
        parts.append(
            ContextPart(
                "system",
                f"Summary of messages up to #{summary_through}",
                "Summary of earlier events in this campaign:\n" + summary,
            )
        )
    return parts


def tool_definition_parts(rule_set: RuleSet) -> list[ContextPart]:
    """Tool definitions are sent beside the messages, so they are recorded as their own parts."""
    specs = tool_specs(rule_set.tool_calls) or []
    return [
        ContextPart(
            "tool definition",
            f"Tool '{spec['function']['name']}' from rule set '{rule_set.id}'",
            json.dumps(spec["function"], indent=2),
        )
        for spec in specs
    ]


def _message_source(message: ChatMessage) -> str:
    if message.role == "user":
        return f"Message #{message.id} (player)"
    return f"Message #{message.id} (GM, {message.model or 'unknown model'})"


@dataclass(frozen=True)
class ContextUsage:
    """Tokens the next prompt would start with, against the window size."""

    used: int
    limit: int

    @property
    def summary_at(self) -> int:
        return int(self.limit * COMPRESS_AT)


def context_usage(
    campaign: Campaign,
    state: CampaignState,
    history: Sequence[ChatMessage],
    rule_set: RuleSet | None,
    options: CampaignSpec,
    limit: int,
    world_rules: Mapping[str, str],
    unattached_world_rules: Mapping[str, str] | None = None,
) -> ContextUsage:
    unsummarized = [m for m in history if m.id > state.through]
    all_rules = {
        **(rule_set.rules if rule_set else {}),
        **(rule_set.attached_rules if rule_set else {}),
        **world_rules,
    }
    used = sum(
        part.tokens
        for part in system_parts(
            campaign.rule_set,
            {**(rule_set.rules if rule_set else {}), **(unattached_world_rules or {})},
            rule_set.important if rule_set else frozenset(),
            state.summary,
            state.through,
            world_state_text(
                state,
                options,
                rule_set.grid if rule_set else None,
                rule_set.combat if rule_set else None,
                rule_set.conditions if rule_set else None,
                clock_rules=rule_set.clock if rule_set else None,
                inventory=rule_set.inventory if rule_set else None,
                hooks=rule_set.hooks if rule_set else None,
                counters=rule_set.counters if rule_set else None,
            ),
            present_rules_text(state, all_rules),
        )
    )
    used += sum(estimate_tokens(m.content) for m in unsummarized)
    return ContextUsage(used=used, limit=limit)


def _verbatim_start(history: Sequence[ChatMessage], budget: int) -> int:
    """Index of the first message in the newest run that fits within `budget` tokens."""
    used = 0
    start = len(history)
    for index in range(len(history) - 1, -1, -1):
        cost = estimate_tokens(history[index].content)
        if used + cost > budget:
            break
        used += cost
        start = index
    return start


def _keep_newest(text: str, budget_tokens: int) -> str:
    """Keep the newest text that fits the budget, starting at a word boundary."""
    limit = budget_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    first_space = kept.find(" ")
    return kept[first_space + 1 :] if first_space != -1 else kept


async def _repeated_place(
    provider: Provider, model_name: str, names: Sequence[str], name: str
) -> str | None:
    """The existing place that `name` repeats, or None. The model judges the wording, so a name
    that repeats a place in other words is caught too."""
    if not names:
        return None
    listing = "\n".join(f"- {existing}" for existing in names)
    messages = [
        {"role": "system", "content": NAME_CHECK_PROMPT},
        {"role": "user", "content": f"Existing places:\n{listing}\n\nNew place: {name}"},
    ]
    reply = (await complete(provider, model_name, messages)).strip()
    if not reply.upper().startswith("REPEAT:"):
        return None
    named = reply.split(":", 1)[1].strip().casefold()
    return next((existing for existing in names if existing.casefold() == named), None)


async def _summarize(
    provider: Provider,
    model_name: str,
    previous: str,
    messages: Sequence[ChatMessage],
    budget_tokens: int,
    parts: list[ContextPart],
) -> str:
    transcript = "\n".join(f"{m.role}: {m.content}" for m in messages)
    words = int(budget_tokens * WORDS_PER_TOKEN)
    prompt = [
        {"role": "system", "content": f"{SUMMARY_PROMPT} Keep it under {words} words."},
        {
            "role": "user",
            "content": f"{SUMMARY_HEADING}\n{previous or '(none)'}\n\nNew events:\n{transcript}",
        },
    ]
    summary = (await complete(provider, model_name, prompt)).strip()
    parts.extend(
        [
            ContextPart("user", "Summary request", "\n\n".join(m["content"] for m in prompt)),
            ContextPart("assistant", "Summary reply", summary),
        ]
    )
    # Models often repeat the heading they were given. It is not part of the summary.
    if summary.startswith(SUMMARY_HEADING):
        summary = summary[len(SUMMARY_HEADING) :].strip()
    if not summary:
        raise LLMError("the model returned an empty summary")
    return _keep_newest(summary, budget_tokens)


@dataclass(frozen=True)
class Status:
    """What the GM is doing now, shown while the reply is pending."""

    text: str


@dataclass(frozen=True)
class Delta:
    """A piece of the GM's reply text, as it arrives."""

    text: str


@dataclass(frozen=True)
class LogLine:
    """An entry of the Log, shown in the chat between the messages. `id` is the id of the event
    that recorded it."""

    id: int
    content: str
    role: str = "log"
    hidden: bool = False


@dataclass(frozen=True)
class Logged:
    """Something that happened during the reply and is shown in the Log, such as a roll."""

    text: str
    hidden: bool = False


@dataclass(frozen=True)
class Prompt:
    """The reply is paused until the player rolls `dice` or types a reply to `text`.

    `details`, if set, is shown with the prompt, such as the modifiers for a roll."""

    text: str
    dice: str
    details: str | None = None


@dataclass(frozen=True)
class Saved:
    """The turn is stored. `event_id` is the GM's reply."""

    event_id: int


@dataclass(frozen=True)
class NextMessage:
    """The reply goes on in a new message, such as the story after an out-of-character reply."""


TurnEvent = Status | Delta | Logged | Prompt | Saved | NextMessage


def _add_instruction(
    messages: list[dict[str, Any]], context: list[ContextPart], source: str, text: str
) -> None:
    messages.append({"role": "system", "content": text})
    context.append(ContextPart("system", source, text))


def _add_speech(
    messages: list[dict[str, Any]], context: list[ContextPart], speech: list[dict[str, str]]
) -> None:
    lines = [{"speaker": line["speaker"], "dialogue": line["dialogue"]} for line in speech]
    payload = json.dumps({"queued_dialogue": lines}, ensure_ascii=False, indent=2)
    _add_instruction(messages, context, "Queued dialogue", payload)


def _queued_dialogue_note(speech: list[dict[str, str]]) -> dict[str, str]:
    """A note for the state step about the dialogue already queued this reply. It is not recorded,
    so the state step can see what it has done and amend a line rather than repeat it."""
    lines = "\n".join(f'{line["speaker"]}: "{line["dialogue"]}"' for line in speech)
    return {
        "role": "system",
        "content": "Dialogue already queued this reply, in the order it was said:\n"
        f"{lines}\nDo not speak these NPCs again. To amend a queued line, call speak with "
        "replace: true.",
    }


def _text_message(content: str, reasoning: str) -> dict[str, Any]:
    """A plain assistant message. Thinking models reject a request whose assistant message after
    the last user message has no reasoning_content key, even when the reasoning is empty."""
    return {"role": "assistant", "content": content, "reasoning_content": reasoning}


def _history_message(m: ChatMessage) -> dict[str, Any]:
    """A chat message as the model sees it. A GM reply keeps the reasoning it was written with."""
    if m.role == "assistant":
        return _text_message(m.content, m.reasoning)
    return {"role": m.role, "content": m.content}


def _tool_call_message(step: ChatReply) -> dict[str, Any]:
    """The assistant message that records a step's tool requests, as the model sees it. Thinking
    models reject it with HTTP 400 when the key is missing, even if the reasoning is empty."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": step.text or None,
        "tool_calls": [
            {
                "id": request.id,
                "type": "function",
                "function": {"name": request.name, "arguments": request.arguments},
            }
            for request in step.tool_requests
        ],
    }
    message["reasoning_content"] = step.reasoning
    return message


def _dicts(parts: Sequence[ContextPart]) -> list[dict[str, str]]:
    return [part.to_dict() for part in parts]


def _status_for(request: ToolRequest) -> str:
    if request.name != "speak":
        return f"Running {request.name}..."
    try:
        speaker = json.loads(request.arguments)["npc"]
    except (ValueError, KeyError, TypeError):
        return "Writing dialogue..."
    return f"Writing dialogue for {speaker}..."


def _tool_ran(round_no: int, request: ToolRequest, result: ToolRun) -> ToolRan:
    return ToolRan(
        request_id=request.id,
        messages=[{"role": "tool", "tool_call_id": request.id, "content": result.output}],
        context=_dicts(
            [
                ContextPart(
                    "assistant",
                    f"GM tool call (round {round_no})",
                    f"{request.name}({request.arguments})",
                ),
                ContextPart(
                    "tool", f"Result of '{request.name}' (round {round_no})", result.output
                ),
            ]
        ),
        activity=result.activity,
    )


def _record(engine: Engine, campaign_id: int, *events: GameEvent) -> int:
    """Record events in one commit. Returns the id of the last one."""
    with session_scope(engine) as db:
        event_id = 0
        for event in events:
            event_id = record(db, campaign_id, event)
        db.commit()
    return event_id


def _state(engine: Engine, campaign_id: int, before: int | None = None) -> CampaignState:
    with session_scope(engine) as db:
        return load_state(db, campaign_id, before)


def _newest(engine: Engine, campaign_id: int) -> int:
    with session_scope(engine) as db:
        return last_event_id(db, campaign_id)


def _turn(state: CampaignState) -> Turn:
    if state.turn is None:
        raise RuntimeError("no turn is open")
    return state.turn


@dataclass
class _Run:
    """What the steps of one reply need: where they are recorded, the model that writes them, and
    the tools they can call. `cursor` is the last event whose Log entries were shown."""

    engine: Engine
    campaign_id: int
    model: str
    rule_set: RuleSet
    context_tokens: int
    provider: Provider
    model_name: str
    ctx: ToolContext
    cursor: int
    opening: str
    # The world's rules that no place, NPC, or monster attaches. They are listed for the GM as
    # campaign rules, so lookup_rule can read them.
    unattached_rules: Mapping[str, str] = field(default_factory=dict)

    @property
    def tool_calls(self) -> Mapping[str, ToolCall]:
        return self.rule_set.tool_calls


def _tool_context(
    engine: Engine,
    library: ContentLibrary,
    rng: random.Random,
    provider: Provider,
    model_name: str,
    campaign_id: int,
    rule_set: RuleSet,
    world: WorldContent,
) -> ToolContext:
    async def speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        await _speak(
            engine,
            provider,
            model_name,
            campaign_id,
            speaker,
            profile,
            intent,
            replace,
            rule_set.sentence_cap,
        )

    def ooc(narrate: bool) -> None:
        _record(engine, campaign_id, OocStarted(narrate=narrate))

    world_tools = WorldTools(
        engine,
        campaign_id,
        {**library.core.tables, **world.tables},
        rng,
        rule_set.character,
        grid=rule_set.grid,
        terrain=rule_set.terrain,
        encounter_objects=rule_set.encounter_objects,
        encounter_templates=rule_set.encounter_templates,
        combat=rule_set.combat,
        monsters={**rule_set.monsters, **world.monsters},
        features={**world.features},
        checks=rule_set.checks,
        conditions=rule_set.conditions,
        moves=rule_set.moves,
        counters=rule_set.counters,
        advancements=rule_set.advancements,
        periods=rule_set.periods,
        tallies=rule_set.tallies,
        relations=rule_set.relations,
        dice_tables={**rule_set.dice_tables, **world.dice_tables},
        clock_rules=rule_set.clock,
        rests=rule_set.rests,
        interrupted_rest=rule_set.interrupted_rest,
        hooks=rule_set.hooks,
        inventory=rule_set.inventory,
        item_sizes=rule_set.item_sizes,
        magic=rule_set.magic,
        marketplace=rule_set.marketplace,
        plus=rule_set.plus,
        npc_hp=rule_set.npc_hp,
        npc_hp_stat=rule_set.npc_hp_stat,
        travel=rule_set.travel,
        wilderness=rule_set.wilderness,
        world_terrain=rule_set.world_terrain,
    )

    async def check_new_location(name: str) -> None:
        repeated = await _repeated_place(provider, model_name, world_tools.place_names(), name)
        if repeated is not None:
            raise InvalidInput(
                f"there is already a location called {repeated!r}; use discover_location to reveal it"
            )

    return ToolContext(
        world=world_tools,
        speak=speak,
        ooc=ooc,
        rules={**rule_set.rules, **rule_set.attached_rules, **world.rules},
        lookup_tools={
            tool.name: brief(tool.description)
            for tool in rule_set.tool_calls.values()
            if tool.lookup
        },
        check_new_location=check_new_location,
    )


def _start_run(
    engine: Engine,
    providers: ProviderFactory,
    library: ContentLibrary,
    rule_set: RuleSet,
    campaign: Campaign,
    context_tokens: int,
    rng: random.Random | None,
) -> _Run:
    provider, model_name = providers(campaign.model)
    world = library.world_content(campaign.world_template, campaign.rule_set)
    return _Run(
        engine=engine,
        campaign_id=campaign.id,
        model=campaign.model,
        rule_set=rule_set,
        context_tokens=context_tokens,
        provider=provider,
        model_name=model_name,
        ctx=_tool_context(
            engine,
            library,
            rng if rng is not None else random.Random(),
            provider,
            model_name,
            campaign.id,
            rule_set,
            world,
        ),
        cursor=_newest(engine, campaign.id),
        opening=opening_prompt(library.world_templates.get(campaign.world_template)),
        unattached_rules=world.unattached_rules,
    )


def _turn_log(engine: Engine, campaign_id: int, after: int) -> list[str]:
    """The Log lines recorded after event `after`, in order."""
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, after, Channel.LOG)
    return [
        text
        for row in rows
        if not is_hidden(row.kind) and (text := decode(row).render()) is not None
    ]


def _turn_has_day_end(engine: Engine, campaign_id: int, opener: int) -> bool:
    """Whether the clock moves into a new day since the player's message, which ends the day the
    journal records. A jump over several days counts once. Hidden events count, since the journal
    keeps what the player does not see."""
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, opener)
    for row in rows:
        event = decode(row)
        if isinstance(event, ClockAdvanced) and event.new_day:
            return True
    return False


def _world_text(run: _Run, state: CampaignState) -> str:
    rule_set = run.rule_set
    return world_state_text(
        state,
        rule_set.campaign,
        rule_set.grid,
        rule_set.combat,
        rule_set.conditions,
        clock_rules=rule_set.clock,
        inventory=rule_set.inventory,
        hooks=rule_set.hooks,
        counters=rule_set.counters,
    )


def _narration_messages(
    run: _Run, turn: Turn, log: list[str] | None = None, heading: str = "Turn log, in order:"
) -> list[dict[str, Any]]:
    """What the narration step reads. The turn's opening system message predates the moves made
    in the turn, so the scene is rebuilt from the state as it is now, which also brings in the
    rules for anything that came into the scene. The turn log follows the conversation, so the
    narration covers each entry. A state step's prose is a draft written before the tools ran, so
    the narration sees the tool calls but not that prose. `log` replaces the turn's own Log lines."""
    state = _state(run.engine, run.campaign_id)
    scene = system_parts(
        run.rule_set.id,
        {**run.rule_set.rules, **run.unattached_rules},
        run.rule_set.important,
        state.summary,
        state.through,
        _world_text(run, state),
        present_rules_text(state, run.ctx.rules),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in scene)},
        *(_without_draft(m) for m in turn.messages[1:]),
    ]
    log = _turn_log(run.engine, run.campaign_id, turn.opener) if log is None else log
    if log:
        lines = "\n".join(f"{number}. {line}" for number, line in enumerate(log, 1))
        entry = {"role": "system", "content": f"{heading}\n{lines}"}
        if messages[-1]["content"] == narrate_instruction(run.rule_set):
            messages.insert(len(messages) - 1, entry)
        else:
            messages.append(entry)
    return messages


def _without_draft(message: dict[str, Any]) -> dict[str, Any]:
    """A tool-call message minus the prose the state step wrote with it. The prose was written
    before the tools ran, so it can name outcomes that never happened."""
    if message.get("role") == "assistant" and message.get("tool_calls"):
        return {**message, "content": None}
    return message


def _flush(run: _Run) -> list[Logged]:
    """The Log entries recorded since the last flush, which the player has not been shown yet."""
    with session_scope(run.engine) as db:
        rows = events_since(db, run.campaign_id, run.cursor, Channel.LOG)
    if rows:
        run.cursor = rows[-1].id or run.cursor
    return [
        Logged(text, hidden=is_hidden(row.kind))
        for row in rows
        if (text := decode(row).render()) is not None
    ]


async def _speak(
    engine: Engine,
    provider: Provider,
    model_name: str,
    campaign_id: int,
    speaker: str,
    profile: str,
    intent: str,
    replace: bool,
    sentence_cap: int | None,
) -> None:
    """Ask the speech step for an NPC's words and record them for the narration. The speech step
    sees the conversation as text, without the tool calls, so it writes dialogue rather than
    narration. Each NPC gets one queued line per reply, so the narration never repeats a speaker.
    With `replace`, the new line takes the queued line's place; a failed attempt keeps the old one."""
    state = _state(engine, campaign_id)
    turn = _turn(state)
    queued = next(
        (line for line in turn.speech if line["speaker"].casefold() == speaker.casefold()), None
    )
    if queued and not replace:
        raise InvalidInput(
            f"{speaker} already has dialogue for this reply; put everything they say in one intent, "
            "or pass replace: true to rewrite that line"
        )
    # The speech step gets no rules or tool instructions: it only writes words, and those
    # instructions get copied into dialogue as tool-call markup.
    system_parts = [speech_instruction(sentence_cap)]
    if state.summary:
        system_parts.append("Summary of earlier events in this campaign:\n" + state.summary)
    system_parts.append(profile)
    if queued:
        system_parts.append(
            f"The line already queued for {speaker}, which this intent replaces: "
            f"{queued['dialogue']}"
        )
    system_parts.append(f"Intent: {intent}")
    system = "\n\n".join(system_parts)
    conversation = [
        m for m in turn.messages[1:] if m["role"] in ("user", "assistant") and "tool_calls" not in m
    ]
    label = f"Speech step (round {turn.rounds})"
    speech = [{"role": "system", "content": system}, *conversation]
    reply = ""
    for _ in range(SPEECH_ATTEMPTS):
        reply = (await complete(provider, model_name, speech)).strip()
        if reply:
            break
    malformed = SPOKEN_MARKUP.search(reply) is not None
    _record(
        engine,
        campaign_id,
        SpeechWritten(
            speaker=speaker,
            dialogue="" if malformed else reply,
            replace=replace,
            context=_dicts(
                [
                    ContextPart("system", label, system),
                    ContextPart("assistant", f"{label} reply", reply),
                ]
            ),
        ),
    )
    if malformed:
        raise InvalidInput(f"the speech step wrote a tool call instead of {speaker}'s words")
    if not reply:
        raise LLMError("the speech step returned no dialogue")


async def _begin(run: _Run, turn: Turn) -> AsyncIterator[TurnEvent]:
    """Set up the model's context for the turn. Once the prompt reaches the compression point, the
    older part of the chat is folded into the summary first."""
    state = _state(run.engine, run.campaign_id)
    rule_set = run.rule_set
    summary, watermark = state.summary, state.through
    world_text = _world_text(run, state)
    message = run.opening if turn.opening else turn.text
    history = [
        m for m in list_messages(run.engine, run.campaign_id) if watermark < m.id < turn.opener
    ]
    present_text = present_rules_text(state, run.ctx.rules)
    prompt_tokens = sum(
        p.tokens
        for p in system_parts(
            rule_set.id,
            {**rule_set.rules, **run.unattached_rules},
            rule_set.important,
            summary,
            watermark,
            world_text,
            present_text,
        )
    )
    prompt_tokens += estimate_tokens(message) + sum(estimate_tokens(m.content) for m in history)
    summary_record: list[ContextPart] = []
    events: list[GameEvent] = []
    if prompt_tokens >= run.context_tokens * COMPRESS_AT:
        start = _verbatim_start(history, int(run.context_tokens * KEEP_RECENT))
        if start > 0:
            folded = history[:start]
            yield Status("Summarizing older messages...")
            summary = await _summarize(
                run.provider,
                run.model_name,
                summary,
                folded,
                int(run.context_tokens * SUMMARY_SHARE),
                summary_record,
            )
            watermark = folded[-1].id
            history = history[start:]
            events.append(SummaryUpdated(summary=summary, through=watermark))
    if turn.opening:
        label = "Opening instruction"
    elif turn.actor is not None:
        label = "Encounter instruction"
    else:
        label = "This message (player)"
    player = ContextPart("user", label, message)
    system = system_parts(
        rule_set.id,
        {**rule_set.rules, **run.unattached_rules},
        rule_set.important,
        summary,
        watermark,
        world_text,
        present_text,
    )
    conversation = [
        *(ContextPart(m.role, _message_source(m), m.content) for m in history),
        player,
    ]
    context = [*system, *tool_definition_parts(rule_set), *conversation]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in system)},
        *(_history_message(m) for m in history),
        {"role": player.role, "content": player.content},
    ]
    round_start = len(context)
    context.append(ContextPart("system", "State instruction", state_instruction(run.rule_set)))
    events.append(
        TurnBegun(
            head=_dicts([*summary_record, player]),
            context=_dicts(context),
            messages=messages,
            round_start=round_start,
        )
    )
    _record(run.engine, run.campaign_id, *events)


async def _write_journal(
    run: _Run, turn: Turn, intro: str = "", reasoning: str = ""
) -> AsyncIterator[TurnEvent]:
    """Let the GM write what happened since the journal was last written into the journal before
    narrating the turn. That log is what the step sees, along with the turn's messages. The
    narrate prompt is left out of the view, since narration has not happened yet. An opening's
    introduction is passed as intro, since it is written before the journal step and the player
    reads it. The journal tools record their own events, so the turn's messages and Log are left
    as they are."""
    journal_tools = tool_specs(run.tool_calls, journal_step=True)
    if journal_tools is None:
        return
    yield Status("Updating the journal...")
    state = _state(run.engine, run.campaign_id)
    log = _turn_log(run.engine, run.campaign_id, state.journal_through or turn.opener)
    _record(run.engine, run.campaign_id, JournalStarted())
    instruction = f"{JOURNAL_INSTRUCTION}\n\nCampaign day {state.clock.day}. "
    instruction += (
        f"The journal so far:\n{state.journal}" if state.journal else "The journal is empty so far."
    )
    narrate = narrate_instruction(run.rule_set)
    view: list[dict[str, Any]] = [
        *(
            m
            for m in _narration_messages(
                run, turn, log=log, heading="Log since the journal was last written, in order:"
            )
            if m.get("content") != narrate
        ),
    ]
    if intro:
        view.append(_text_message(intro, reasoning))
    view.append({"role": "system", "content": instruction})
    for _round in range(JOURNAL_ROUNDS):
        step = await run.provider.chat(run.model_name, view, journal_tools)
        if not step.tool_requests:
            return
        view.append(_tool_call_message(step))
        for request in step.tool_requests:
            result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(result, Ask):
                raise LLMError("the journal step asked the player a question")
            view.append({"role": "tool", "tool_call_id": request.id, "content": result.output})


async def _drive(run: _Run) -> AsyncIterator[TurnEvent]:
    """Run the reply until it is recorded or paused. Log entries are yielded as the steps that
    recorded them finish. A pause is recorded before the player hears of it."""
    tools = tool_specs(run.tool_calls)
    state = {"role": "system", "content": state_instruction(run.rule_set)}
    while True:
        turn = _turn(_state(run.engine, run.campaign_id))
        if turn.queue:
            request = turn.queue[0]
            yield Status(_status_for(request))
            result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(result, Ask):
                _record(
                    run.engine,
                    run.campaign_id,
                    PromptShown(result.text, result.dice, result.details),
                )
                for entry in _flush(run):
                    yield entry
                yield Prompt(result.text, result.dice, result.details)
                return
            _record(run.engine, run.campaign_id, _tool_ran(turn.rounds, request, result))
            for entry in _flush(run):
                yield entry
            continue
        if turn.narrating:
            break
        if turn.ooc:
            yield Status("Replying out of character...")
            round_no = turn.rounds + 1
            view = list(turn.messages)
            if turn.speech:
                view.append(_queued_dialogue_note(turn.speech))
            view.append({"role": "system", "content": OOC_INSTRUCTION})
            step: ChatReply | None = None
            async for item in run.provider.stream(run.model_name, view, tools):
                if isinstance(item, TextDelta):
                    yield Delta(item.text)
                else:
                    step = item
            if step is None:
                raise LLMError("the model returned no reply")
            if step.tool_requests:
                context = []
                if step.text:
                    context.append(
                        ContextPart(
                            "assistant", f"GM out-of-character text (round {round_no})", step.text
                        )
                    )
                _record(
                    run.engine,
                    run.campaign_id,
                    StateStepMade(
                        requests=[asdict(request) for request in step.tool_requests],
                        messages=[_tool_call_message(step)],
                        context=_dicts(context),
                    ),
                )
                continue
            reply = step.text.strip()
            if not reply:
                raise LLMError("the model returned an empty reply")
            if turn.ooc_narrate:
                messages: list[dict[str, Any]] = [_text_message(reply, step.reasoning)]
                context = [ContextPart("assistant", "GM out-of-character reply", reply)]
                if turn.speech:
                    _add_speech(messages, context, turn.speech)
                _add_instruction(
                    messages, context, "Narrate instruction", narrate_instruction(run.rule_set)
                )
                _record(
                    run.engine,
                    run.campaign_id,
                    StateStepMade(requests=[], messages=messages, context=_dicts(context)),
                    ChatGM(
                        content=reply,
                        model=run.model,
                        final=False,
                        reasoning=step.reasoning,
                    ),
                )
                yield NextMessage()
                continue
            if turn.opening:
                async for event in _write_journal(run, turn, reply, step.reasoning):
                    yield event
            event_id = _record(
                run.engine,
                run.campaign_id,
                ChatGM(content=reply, model=run.model, reasoning=step.reasoning),
            )
            yield Saved(event_id)
            return
        yield Status("Managing state...")
        round_no = turn.rounds + 1
        view = [*turn.messages, state]
        if turn.speech:
            view.append(_queued_dialogue_note(turn.speech))
        step = await run.provider.chat(run.model_name, view, tools)
        if not step.tool_requests:
            messages: list[dict[str, Any]] = []
            context: list[ContextPart] = []
            if step.text.strip():
                context.append(
                    ContextPart(
                        "assistant",
                        f"GM state step (round {round_no}), not shown to the narration",
                        step.text,
                    )
                )
            if turn.speech:
                _add_speech(messages, context, turn.speech)
            _add_instruction(
                messages, context, "Narrate instruction", narrate_instruction(run.rule_set)
            )
            _record(
                run.engine,
                run.campaign_id,
                StateStepMade(requests=[], messages=messages, context=_dicts(context)),
            )
            continue
        context = []
        if step.text:
            context.append(ContextPart("assistant", f"GM text (round {round_no})", step.text))
        _record(
            run.engine,
            run.campaign_id,
            StateStepMade(
                requests=[asdict(request) for request in step.tool_requests],
                messages=[_tool_call_message(step)],
                context=_dicts(context),
            ),
        )

    if _turn_has_day_end(run.engine, run.campaign_id, turn.opener):
        async for event in _write_journal(run, turn):
            yield event
    yield Status("Narrating...")
    messages = _narration_messages(run, turn)
    reply = ""
    for _attempt in range(NARRATION_ATTEMPTS):
        narration: ChatReply | None = None
        async for item in run.provider.stream(run.model_name, messages, None):
            if isinstance(item, TextDelta):
                yield Delta(item.text)
            else:
                narration = item
        if narration is None:
            raise LLMError("the model returned no reply")
        reply = narration.text.strip()
        if reply:
            break
    if not reply:
        raise LLMError("the model returned an empty reply")
    event_id = _record(
        run.engine,
        run.campaign_id,
        ChatGM(content=reply, model=run.model, reasoning=narration.reasoning),
    )
    yield Saved(event_id)


async def _carry_on(run: _Run) -> AsyncIterator[TurnEvent]:
    turn = _turn(_state(run.engine, run.campaign_id))
    if not turn.begun:
        async for event in _begin(run, turn):
            yield event
    async for event in _drive(run):
        yield event


async def _answer(run: _Run, answer: RollAnswer | TextAnswer | None) -> AsyncIterator[TurnEvent]:
    """Give the waiting tool the player's answer, record its result, then carry the reply on."""
    if answer is not None:
        turn = _turn(_state(run.engine, run.campaign_id))
        request = turn.queue[0]
        result = await run_tool(run.tool_calls, request, answer, ctx=run.ctx)
        assert isinstance(result, ToolRun)
        _record(run.engine, run.campaign_id, _tool_ran(turn.rounds, request, result))
        for entry in _flush(run):
            yield entry
        if isinstance(answer, TextAnswer):
            speaker = speaker_name(_state(run.engine, run.campaign_id))
            _record(run.engine, run.campaign_id, ChatPlayer(content=answer.text, speaker=speaker))
    async for event in _carry_on(run):
        yield event


async def _guarded(run: _Run, steps: AsyncIterator[TurnEvent]) -> AsyncIterator[TurnEvent]:
    """Pass the steps along. If one fails, record why, so the reply can be continued or rolled
    back. A step that is cancelled or abandoned records nothing, and its turn stays open."""
    try:
        async for event in steps:
            yield event
    except Exception as exc:
        message = str(exc) if isinstance(exc, LLMError) else "an unexpected error"
        _record(run.engine, run.campaign_id, TurnFailed(error=message))
        raise


async def stream_message(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    text: str,
    rng: random.Random | None = None,
) -> AsyncIterator[TurnEvent]:
    """Send a user message, to the campaign's model, and yield what happens while the GM replies.

    Yields Status while the GM works, Logged for each Log entry the turn adds, Delta for the
    narration only, and Prompt when the reply needs the player to roll or type something. The
    reply then continues through `resume_turn`. Saved comes once the reply is recorded.

    The message is recorded before the reply starts, so it survives a failed reply. A failed reply
    stays open: `resume_turn` with "continue" retries it, and rolling back removes it."""
    text = (text or "").strip()
    if not text:
        raise InvalidInput("message cannot be empty")
    if len(text) > MAX_MESSAGE:
        raise InvalidInput(f"message must be at most {MAX_MESSAGE} characters")
    campaign = get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _exclusive(campaign.id):
        open_turn = _state(engine, campaign.id).turn
        if open_turn is not None:
            if open_turn.prompt is not None:
                raise InvalidInput("answer the GM's prompt before sending another message")
            raise InvalidInput(
                "the GM's reply is unfinished; continue it or roll back before sending another message"
            )
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        player = _state(engine, campaign.id).pc()
        if player is None:
            raise InvalidInput("create your character before playing")
        if player.out_of_play:
            state = "is dead" if player.dead else "has left the story"
            raise InvalidInput(
                f"your character {state}: choose a new character or a hireling first"
            )
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        speaker = speaker_name(_state(engine, campaign.id))
        _record(engine, campaign.id, ChatPlayer(content=text, speaker=speaker))
        async for event in _guarded(run, _carry_on(run)):
            yield event


async def resume_turn(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    action: str,
    text: str = "",
    rng: random.Random | None = None,
) -> AsyncIterator[TurnEvent]:
    """Continue an open reply with the player's answer, using the campaign's current model.

    `action` is "roll" to roll the prompt's dice, "text" to answer with free-form `text`, or
    "continue" to retry a reply that stopped on an error or was interrupted. The answer is
    recorded before the reply continues, so a failure later does not roll again."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _exclusive(campaign.id):
        turn = _state(engine, campaign.id).turn
        if turn is None:
            raise InvalidInput("there is no reply waiting for an answer")
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        answer: RollAnswer | TextAnswer | None = None
        if action == "continue":
            if turn.prompt is not None:
                raise InvalidInput("answer the GM's prompt first")
        elif turn.prompt is None:
            raise InvalidInput("there is nothing to answer")
        elif action == "roll":
            answer = RollAnswer(roll_total(turn.prompt.dice, rng, plus=rule_set.plus))
        elif action == "text":
            clean = (text or "").strip()
            if not clean:
                raise InvalidInput("the reply cannot be empty")
            if len(clean) > MAX_MESSAGE:
                raise InvalidInput(f"the reply must be at most {MAX_MESSAGE} characters")
            answer = TextAnswer(clean)
        else:
            raise InvalidInput("that action is not available")
        actor = turn.actor
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        async for event in _guarded(run, _answer(run, answer)):
            yield event
        if actor is not None:
            _close_npc_turn(engine, campaign.id, actor)


def _close_npc_turn(engine: Engine, campaign_id: int, name: str) -> None:
    """End an NPC's turn once the GM's reply has finished, if the GM did not call end_turn itself,
    so the order can move on. A reply still waiting on a prompt keeps the turn open."""
    state = _state(engine, campaign_id)
    encounter = state.encounter
    if state.turn is not None or encounter is None or encounter.acting is None:
        return
    if name_key(encounter.acting) == name_key(name):
        with session_scope(engine) as db:
            encounters.end_turn(db, campaign_id, name, by_player=False)


async def start_encounter_turn(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    name: str,
    rng: random.Random | None = None,
) -> AsyncIterator[TurnEvent]:
    """Start the NPC whose turn is next in the encounter order. The GM plays it out and it is
    streamed like a reply. A player character's turn starts on its own and ends with
    `end_player_turn`."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _exclusive(campaign.id):
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        with session_scope(engine) as db:
            character = encounters.begin_turn(db, campaign.id, name)
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        async for event in _guarded(run, _carry_on(run)):
            yield event
        _close_npc_turn(engine, campaign.id, character.name)


def end_player_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """End a player character's turn. Refused while the GM's reply is still open."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id):
        if _state(engine, campaign_id).turn is not None:
            raise InvalidInput("the GM's reply is unfinished; finish it before ending your turn")
        with session_scope(engine) as db:
            encounters.end_turn(db, campaign_id, name, by_player=True)


@dataclass(frozen=True)
class PendingView:
    """An open reply that needs attention: its prompt, or why it stopped. The player's words and
    the rest of the turn are already in the transcript and the Log. `auto_continue` is set for the
    campaign's opening before it has failed, so the page can start it without the player."""

    prompt: Ask | None
    error: str | None = None
    opening: bool = False
    auto_continue: bool = False


def pending_turn(engine: Engine, owner_id: int, campaign_id: int) -> PendingView | None:
    """The open reply's prompt, or why it stopped. None when no reply is open, or while one is
    running in this process."""
    get_campaign(engine, owner_id, campaign_id)
    with _busy_lock:
        if campaign_id in _busy_campaigns:
            return None
    turn = _state(engine, campaign_id).turn
    if turn is None:
        return None
    if turn.prompt is not None:
        return PendingView(prompt=turn.prompt, opening=turn.opening)
    auto = turn.opening and turn.failed is None
    return PendingView(
        prompt=None,
        error=turn.failed or "The GM's reply did not finish.",
        opening=turn.opening,
        auto_continue=auto,
    )


async def send_message(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    text: str,
) -> None:
    """Send a user message and wait for the GM's reply to be stored."""
    async for _ in stream_message(
        engine,
        providers,
        allowed_models,
        library,
        context_tokens,
        owner_id,
        campaign_id,
        text,
    ):
        pass


def rollback_to(engine: Engine, owner_id: int, campaign_id: int, event_id: int) -> None:
    """Return the campaign to just after the GM reply `event_id`. Later events are removed,
    including any open reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        truncate(db, campaign_id, event_id)
        db.commit()


def discard_pending(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Remove the open reply and the player's message that started it, so the player can send
    the message again. Used when a reply cannot be continued. The opening cannot be discarded: it
    is recorded with the player character, and it is continued instead."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        turn = load_state(db, campaign_id).turn
        if turn is None:
            raise NotFound("there is no open reply to discard")
        if turn.opening:
            raise InvalidInput("the campaign's opening cannot be discarded; continue it instead")
        truncate(db, campaign_id, turn.opener - 1)
        db.commit()


def campaign_log(engine: Engine, owner_id: int, campaign_id: int) -> list[str]:
    """The Log's entries, oldest first."""
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.LOG)
        return [text for row in rows if (text := decode(row).render()) is not None]


def _turn_for_reply(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> tuple[Turn | None, str]:
    """The turn that produced a GM reply, as it stood before the reply was recorded, and the
    reply's text. The turn is None when no turn was open, so nothing was recorded for the reply."""
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        reply = decode(event)
        assert isinstance(reply, ChatGM)
        turn = load_state(db, campaign_id, before=event_id).turn
    return turn, reply.content


def get_turn_context(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> list[ContextPart]:
    """The context the model was given for a GM reply."""
    turn, _ = _turn_for_reply(engine, owner_id, campaign_id, event_id)
    return [] if turn is None else list(turn.context)


def get_turn_transcript(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> list[ContextPart]:
    """What happened in the round that produced a GM reply, from the player's message to the
    reply."""
    turn, reply = _turn_for_reply(engine, owner_id, campaign_id, event_id)
    if turn is None:
        return []
    return [
        *turn.head,
        *turn.context[turn.round_start :],
        ContextPart("assistant", "Reply", reply),
    ]
