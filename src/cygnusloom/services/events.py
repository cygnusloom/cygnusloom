"""A campaign's event log: the only stored state of a campaign.

Every change to the world, the chat, and the GM's turn is an event appended with `record`. There
are no tables of current state. `load_state` folds the log into a `CampaignState`, the same way
each time: a rollback removes events, so the next fold no longer sees them.

To add an event, subclass `GameEvent` as a frozen dataclass with a unique `kind`, a `channel`, an
`apply` that changes the state, and a `render` if it appears in the Log. The fields are the JSON
payload. Store resolved values (names, rolled traits, the text the model was given) rather than
the inputs used to compute them, so replaying the log gives the same result. Refer to places and
characters by name, which is unique within a campaign and never changes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, ClassVar

from sqlalchemy import delete, func
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, Event, User
from cygnusloom.llm.base import ToolRequest
from cygnusloom.services.accounts import public_name_for
from cygnusloom.services.state import (  # noqa: F401
    CAMPAIGN_WIDE,
    CHARS_PER_TOKEN,
    DEFAULT_DIMENSION,
    FOES_SIDE,
    MORALE_LABELS,
    NPC_KIND,
    PARTY_SIDE,
    PC_KIND,
    CampaignState,
    CellMark,
    Channel,
    Character,
    Clock,
    ContextPart,
    CustomFeature,
    DebugInjection,
    Encounter,
    EncounterMap,
    Faction,
    FactionAsset,
    Item,
    KnownNpc,
    Note,
    Place,
    Position,
    Project,
    Region,
    RestRecord,
    Storyline,
    Turn,
    WorldMapStyle,
    WorldObject,
    _next_round,
    _player_present,
    _position,
    _remember_npc,
    _skip_defeated,
    current,
    defeat_status,
    estimate_tokens,
    group_by_side,
    marks_from_list,
    modifier_value,
    name_key,
    normalize_journal,
    out_of_fight,
    same_item_name,
    speaker_name,
    token_label,
    token_labels,
)
from cygnusloom.services.tools import Ask

EVENT_TYPES: dict[str, type[GameEvent]] = {}


class GameEvent:
    kind: ClassVar[str]
    # Whether the player's transcript hides events of this kind. An event class declares its own
    # visibility next to its kind, so there is no central list to update when adding an event.
    hidden: ClassVar[bool] = False
    channel: ClassVar[Channel] = Channel.LOG
    # Payload fields that hold another event's id, mapped to the kinds that event must have.
    # Importing a campaign allocates new event ids, so these fields need remapping.
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("kind")
        if not kind:
            raise TypeError(f"{cls.__name__} must set a kind")
        if kind in EVENT_TYPES:
            raise TypeError(f"event kind {kind!r} is already registered")
        EVENT_TYPES[kind] = cls

    def apply(self, state: CampaignState, event_id: int) -> None:
        """Change the state this event affects. Most events affect none."""

    def render(self) -> str | None:
        """The Log text, or None when the Log does not show this event."""
        raise NotImplementedError(f"{type(self).__name__} has no Log text")


def record(db: Session, campaign_id: int, event: GameEvent) -> int:
    """Append an event to the campaign's log and return its id."""
    newest = db.exec(select(func.max(Event.seq)).where(Event.campaign_id == campaign_id)).one()
    row = Event(
        campaign_id=campaign_id,
        seq=(newest or 0) + 1,
        channel=event.channel.value,
        kind=event.kind,
        payload=json.dumps(asdict(event)),
    )
    db.add(row)
    db.flush()
    if row.id is None:
        raise RuntimeError("the event was not assigned an id")
    return row.id


def decode(row: Event) -> GameEvent:
    """The event a row stores. Payload keys the event no longer has are ignored."""
    cls = EVENT_TYPES[row.kind]
    known = {f.name for f in fields(cls)}
    payload = json.loads(row.payload)
    return cls(**{key: value for key, value in payload.items() if key in known})


def load_state(db: Session, campaign_id: int, before: int | None = None) -> CampaignState:
    """Fold the campaign's log into its state. With `before`, only the events before that id."""
    statement = select(Event).where(Event.campaign_id == campaign_id)
    if before is not None:
        statement = statement.where(Event.id < before)
    # The owner is configuration rather than part of the log, but a character with no recorded
    # player belongs to it, so the fold needs it once.
    state = CampaignState(
        owner_id=db.exec(select(Campaign.owner_id).where(Campaign.id == campaign_id)).one_or_none()
    )
    for row in db.exec(statement.order_by(Event.seq)):
        event = decode(row)
        event.apply(state, row.id or 0)
        if state.turn is not None and not isinstance(event, TurnFailed):
            state.turn.failed = None
    _resolve_player_names(db, state)
    return state


def _resolve_player_names(db: Session, state: CampaignState) -> None:
    """Put each player's current display name into the folded state.

    The log stores only the account id. A character with no player id belongs to the campaign
    owner unless it was deliberately unclaimed, which keeps logs recorded before sharing worked
    exactly as they did."""
    player_ids: set[int] = set()
    if state.owner_id is not None:
        player_ids.add(state.owner_id)
    for character in state.characters.values():
        if character.player_id is not None:
            player_ids.add(character.player_id)
    if state.turn is not None and state.turn.player_id is not None:
        player_ids.add(state.turn.player_id)
    names: dict[int, str] = {}
    if player_ids:
        rows = db.exec(select(User.id, User.display_name).where(User.id.in_(player_ids))).all()
        names = {
            int(user_id): public_name_for(int(user_id), display_name)
            for user_id, display_name in rows
            if user_id is not None
        }
    for character in state.characters.values():
        if character.unclaimed:
            character.player_name = ""
            continue
        player_id = character.player_id if character.player_id is not None else state.owner_id
        character.player_name = names.get(player_id, "") if player_id is not None else ""
    if state.turn is not None:
        player_id = state.turn.player_id
        state.turn.player_name = names.get(player_id, "") if player_id is not None else ""


def events_since(
    db: Session, campaign_id: int, after: int = 0, channel: Channel | None = None
) -> list[Event]:
    statement = select(Event).where(Event.campaign_id == campaign_id, Event.id > after)
    if channel is not None:
        statement = statement.where(Event.channel == channel.value)
    return list(db.exec(statement.order_by(Event.seq)))


def last_event_id(db: Session, campaign_id: int) -> int:
    """The id of the campaign's newest event, or 0 when it has none."""
    newest = db.exec(select(func.max(Event.id)).where(Event.campaign_id == campaign_id)).one()
    return newest or 0


def truncate(db: Session, campaign_id: int, after: int) -> None:
    """Remove the campaign's events after `after`."""
    db.exec(delete(Event).where(Event.campaign_id == campaign_id, Event.id > after))


def _open_turn(state: CampaignState) -> Turn:
    if state.turn is None:
        raise RuntimeError("the log has a turn event with no turn open")
    return state.turn


def _parts(items: list[dict[str, str]]) -> list[ContextPart]:
    return [ContextPart(**item) for item in items]


@dataclass(frozen=True)
class RollMade(GameEvent):
    """A generic record of a roll whose outcome a feature or a rule set's module already knows."""

    kind: ClassVar[str] = "roll.made"

    name: str
    roll: str
    total: int
    cause: str = ""

    def render(self) -> str:
        return f"{self.name} rolls {self.roll}: {self.total} ({self.cause})."


@dataclass(frozen=True)
class StatChanged(GameEvent):
    """A generic change to one of a character's current stat scores. `before` and `after` are the
    current values, not the maximums."""

    kind: ClassVar[str] = "character.stat_changed"
    hidden: ClassVar[bool] = True

    name: str
    stat: str
    before: int
    after: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.harm[self.stat] = max(0, character.stats.get(self.stat, 0) - self.after)

    def render(self) -> str:
        return f"{self.name}'s {self.stat.upper()} changes from {self.before} to {self.after}."


@dataclass(frozen=True)
class ResourceChanged(GameEvent):
    """A generic change to a feature count or a counter, such as silver, a charge, or Experience."""

    kind: ClassVar[str] = "character.resource_changed"
    hidden: ClassVar[bool] = True

    name: str
    resource: str
    before: int
    after: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if self.resource in character.counters:
            character.counters[self.resource] = self.after
        else:
            character.features[self.resource] = max(0, self.after)

    def render(self) -> str:
        return f"{self.name}'s {self.resource} changes from {self.before} to {self.after}."


@dataclass(frozen=True)
class ConditionChanged(GameEvent):
    """A generic condition change for a rule-set module or a feature that already decided the
    outcome. Stored conditions are added or removed directly."""

    kind: ClassVar[str] = "character.condition_changed"
    hidden: ClassVar[bool] = True

    name: str
    condition: str
    active: bool
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if self.active:
            if self.condition not in character.conditions:
                character.conditions.append(self.condition)
        elif self.condition in character.conditions:
            character.conditions.remove(self.condition)

    def render(self) -> str:
        state = "gains" if self.active else "loses"
        return f"{self.name} {state} the {self.condition} condition ({self.cause})."


@dataclass(frozen=True)
class LocationAdded(GameEvent):
    kind: ClassVar[str] = "location.added"
    hidden: ClassVar[bool] = True

    name: str
    description: str
    discovered: bool = False
    x: float | None = None
    y: float | None = None
    parent: str | None = None
    icon: str | None = None
    # What the player sees of the place, written without secrets. None when it was never written.
    summary: str | None = None
    # The ids of rules attached to the place. Their text reaches the GM while the player is here.
    rules: list[str] = field(default_factory=list)
    # Authored overland routes from this place, by destination location id.
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.places[name_key(self.name)] = Place(
            self.name,
            self.description,
            self.discovered,
            _position(self.x, self.y),
            self.parent,
            icon=self.icon,
            summary=self.summary,
            rules=tuple(self.rules),
            routes={key: dict(route) for key, route in self.routes.items()},
        )

    def render(self) -> str:
        return f"New location: {self.name}."


@dataclass(frozen=True)
class RegionAdded(GameEvent):
    """An area of ground on the world map. The GM can add one the world's data lacks. Unless it is
    forced, the ground already there is cut away, so regions do not overlap; it is drawn on the
    player's map once it is discovered or a discovered place lies inside it."""

    kind: ClassVar[str] = "region.added"
    hidden: ClassVar[bool] = True

    name: str
    terrain: str
    points: list[list[float]]
    discovered: bool = False
    color: str | None = None
    pattern: str | None = None
    ink: str | None = None
    label: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.regions[name_key(self.name)] = Region(
            self.name,
            self.terrain,
            tuple((x, y) for x, y in self.points),
            discovered=self.discovered,
            color=self.color,
            pattern=self.pattern,
            ink=self.ink,
            label=self.label,
        )

    def render(self) -> str:
        return f"New region: {self.name}."


@dataclass(frozen=True)
class RegionUpdated(GameEvent):
    """A region's ground, outline, discovery, or look after an update. The service resolves every
    field, so apply replaces them all; None keeps the terrain kind's own colour or texture."""

    kind: ClassVar[str] = "region.updated"
    hidden: ClassVar[bool] = True

    name: str
    terrain: str
    points: list[list[float]]
    discovered: bool
    color: str | None = None
    pattern: str | None = None
    ink: str | None = None
    label: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.regions[name_key(self.name)] = Region(
            self.name,
            self.terrain,
            tuple((x, y) for x, y in self.points),
            discovered=self.discovered,
            color=self.color,
            pattern=self.pattern,
            ink=self.ink,
            label=self.label,
        )

    def render(self) -> str:
        return f"Region updated: {self.name}."


@dataclass(frozen=True)
class WorldMapStyleChanged(GameEvent):
    """The player's world map changes palette, ambient light, colours, or detail. The service
    resolves every field, so apply sets them all rather than merging them."""

    kind: ClassVar[str] = "map.world_style_changed"
    hidden: ClassVar[bool] = True

    palette: str
    ambient: str
    background_color: str = ""
    line_color: str = ""
    show_clouds: bool = True
    show_contours: bool = True

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.world_map_style = WorldMapStyle(
            palette=self.palette,
            ambient=self.ambient,
            background_color=self.background_color,
            line_color=self.line_color,
            show_clouds=self.show_clouds,
            show_contours=self.show_contours,
        )

    def render(self) -> str:
        return "World map restyled."


@dataclass(frozen=True)
class PcAdded(GameEvent):
    kind: ClassVar[str] = "pc.added"

    name: str
    location: str | None = None
    pronouns: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    skills: dict[str, int] = field(default_factory=dict)
    attack_shock: dict[str, list[int]] = field(default_factory=dict)
    # The account that plays the character. Older logs leave this out, and those characters
    # belong to the campaign owner. The account's display name is resolved when the log is
    # folded, never stored in the event.
    player_id: int | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)] = Character(
            kind=PC_KIND,
            name=self.name,
            location=self.location,
            description="",
            personality="",
            accent="",
            quirks=[],
            pronouns=self.pronouns,
            stats=dict(self.stats),
            templates=dict(self.templates),
            features=dict(self.features),
            fields=dict(self.fields),
            counters=dict(self.counters),
            skills=dict(self.skills),
            attack_shock={name: list(pair) for name, pair in self.attack_shock.items()},
            player_id=self.player_id,
        )

    def render(self) -> str:
        if self.location is None:
            return f"{self.name} is ready to begin, not yet placed at a location."
        return f"{self.name} begins at {self.location}."


@dataclass(frozen=True)
class CampaignOptionsChosen(GameEvent):
    """The options every character in the campaign shares. Chosen once, before the player
    character, and given to the GM through the state text rather than the chat."""

    kind: ClassVar[str] = "campaign.options"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    options: dict[str, str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.options = dict(self.options)


# BACKWARDS-COMPATIBILITY: 3 - old logs and exports contain campaign.upgraded.
# Campaigns always load the running game's rules and content, so nothing records this any more.
# It stays so old logs decode, and it opens a turn like it did then: the turn events that followed
# it call `_open_turn`, which raises when no turn is open.
@dataclass(frozen=True)
class CampaignUpgraded(GameEvent):
    """A campaign moved to a new game version, recorded before campaigns loaded the running game
    directly. The transcript hides the entry and no story is narrated for it."""

    kind: ClassVar[str] = "campaign.upgraded"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.LOG

    from_version: str
    to_version: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        before = self.from_version or "an earlier version"
        if state.turn is None:
            state.turn = Turn(
                opener=event_id,
                text=(
                    f"The game has been upgraded from version {before} to "
                    f"{self.to_version}. Acknowledge the upgrade out of character."
                ),
                ooc=True,
                ooc_narrate=False,
            )

    def render(self) -> str:
        before = self.from_version or "an earlier version"
        return f"The game was upgraded from version {before} to {self.to_version}."


@dataclass(frozen=True)
class NpcAdded(GameEvent):
    kind: ClassVar[str] = "npc.added"
    hidden: ClassVar[bool] = True

    name: str
    location: str
    description: str
    personality: str
    accent: str
    quirks: list[str]
    pronouns: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    armor: int = 0
    attacks: dict[str, str] = field(default_factory=dict)
    attack_flags: dict[str, list[str]] = field(default_factory=dict)
    attack_shock: dict[str, list[int]] = field(default_factory=dict)
    creature: str = ""
    large_group: bool = False
    story_only: bool = False
    # A named NPC the story treats as important: at 0 HP it is mortally wounded, not dead.
    important: bool = False
    rules: list[str] = field(default_factory=list)
    skills: dict[str, int] = field(default_factory=dict)

    def apply(self, state: CampaignState, event_id: int) -> None:
        npc = Character(
            kind=NPC_KIND,
            name=self.name,
            location=self.location,
            description=self.description,
            personality=self.personality,
            accent=self.accent,
            quirks=list(self.quirks),
            pronouns=self.pronouns,
            stats=dict(self.stats),
            templates=dict(self.templates),
            features=dict(self.features),
            fields=dict(self.fields),
            counters=dict(self.counters),
            armor=self.armor,
            attacks=dict(self.attacks),
            attack_flags={name: list(flags) for name, flags in self.attack_flags.items()},
            attack_shock={name: list(pair) for name, pair in self.attack_shock.items()},
            creature=self.creature,
            large_group=self.large_group,
            story_only=self.story_only,
            important=self.important,
            rules=list(self.rules),
            skills=dict(self.skills),
        )
        state.characters[name_key(self.name)] = npc
        pc = _player_present(state)
        if pc is not None and pc.location == npc.location:
            _remember_npc(state, npc)

    def render(self) -> str:
        return f"New character: {self.name}, at {self.location}."


@dataclass(frozen=True)
class NpcStatted(GameEvent):
    """A story-only NPC is given its stats. The values are the resolved ones."""

    kind: ClassVar[str] = "npc.statted"
    hidden: ClassVar[bool] = True

    name: str
    stats: dict[str, int]
    templates: dict[str, str]
    features: dict[str, int]
    fields: dict[str, str]
    armor: int
    attacks: dict[str, str]
    attack_flags: dict[str, list[str]] = field(default_factory=dict)
    attack_shock: dict[str, list[int]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    skills: dict[str, int] = field(default_factory=dict)

    def apply(self, state: CampaignState, event_id: int) -> None:
        npc = state.characters[name_key(self.name)]
        npc.stats = dict(self.stats)
        npc.templates = dict(self.templates)
        npc.features = dict(self.features)
        npc.fields = dict(self.fields)
        npc.counters = dict(self.counters)
        npc.armor = self.armor
        npc.attacks = dict(self.attacks)
        npc.attack_flags = {name: list(flags) for name, flags in self.attack_flags.items()}
        npc.attack_shock = {name: list(pair) for name, pair in self.attack_shock.items()}
        npc.skills = dict(self.skills)
        npc.story_only = False

    def render(self) -> str:
        return f"Character given stats: {self.name}."


@dataclass(frozen=True)
class HirelingAdded(GameEvent):
    """A hireling joins a party member's company. It has the player character's sheet shape and
    is controlled by the player, paid `wage` a day from its employer's currency."""

    kind: ClassVar[str] = "hireling.added"

    name: str
    employer: str
    wage: int
    location: str
    description: str
    personality: str
    accent: str
    quirks: list[str]
    pronouns: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    skills: dict[str, int] = field(default_factory=dict)
    attack_shock: dict[str, list[int]] = field(default_factory=dict)
    # Generated the way an NPC's are. The hireling's passive defense starts from these.
    counters: dict[str, int] = field(default_factory=dict)
    armor: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)] = Character(
            kind=PC_KIND,
            name=self.name,
            location=self.location,
            description=self.description,
            personality=self.personality,
            accent=self.accent,
            quirks=list(self.quirks),
            pronouns=self.pronouns,
            stats=dict(self.stats),
            templates=dict(self.templates),
            features=dict(self.features),
            fields=dict(self.fields),
            counters=dict(self.counters),
            armor=self.armor,
            skills=dict(self.skills),
            attack_shock={name: list(pair) for name, pair in self.attack_shock.items()},
            hireling=True,
            employer=self.employer,
            wage=self.wage,
        )

    def render(self) -> str:
        return f"{self.name} joins {self.employer}'s company as a hireling, at {self.location}."


@dataclass(frozen=True)
class HirelingDismissed(GameEvent):
    """A hireling leaves its employer's company and becomes an ordinary NPC where it stands."""

    kind: ClassVar[str] = "hireling.dismissed"

    name: str
    cause: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.kind = NPC_KIND
        character.hireling = False
        character.employer = ""
        character.wage = 0
        # The player knew the hireling while they were in the company.
        _remember_npc(state, character)

    def render(self) -> str:
        return f"{self.name} leaves the company: {self.cause}."


@dataclass(frozen=True)
class HirelingPromoted(GameEvent):
    """A hireling takes over as the player character after the old one died. It keeps its sheet
    and gear and stops being paid by its employer. The account that played the old character
    plays the promoted one."""

    kind: ClassVar[str] = "hireling.promoted"

    name: str
    employer: str
    player_id: int | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.hireling = False
        character.employer = ""
        character.wage = 0
        character.player_id = self.player_id

    def render(self) -> str:
        return (
            f"{self.name} takes up the player character's part, leaving {self.employer}'s company."
        )


@dataclass(frozen=True)
class PcUnclaimed(GameEvent):
    """A player left the campaign and their character stays in the story. The character has no
    player; any player at the table may act for them and the GM keeps them in the party."""

    kind: ClassVar[str] = "pc.unclaimed"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.player_id = None
        character.player_name = ""
        character.unclaimed = True

    def render(self) -> str:
        return f"{self.name} has no player; the table shares the part."


@dataclass(frozen=True)
class HirelingUnpaid(GameEvent):
    """The employer could not pay a hireling's wage for the day. The GM decides what the hireling
    does about it."""

    kind: ClassVar[str] = "hireling.unpaid"

    name: str
    employer: str
    wage: int
    currency: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        pass

    def render(self) -> str:
        return (
            f"{self.employer} cannot pay {self.name}'s wage of {self.wage} {self.currency}. "
            "The GM decides whether the hireling stays."
        )


@dataclass(frozen=True)
class ActorMoved(GameEvent):
    kind: ClassVar[str] = "actor.moved"

    name: str
    location: str | None

    def apply(self, state: CampaignState, event_id: int) -> None:
        actor = state.characters[name_key(self.name)]
        actor.location = self.location
        place = state.places.get(name_key(self.location)) if self.location else None
        if place is not None:
            state.places[name_key(place.name)] = replace(place, visited_day=state.clock.day)
        pc = _player_present(state)
        if pc is None or self.location is None:
            return
        if actor.kind == PC_KIND:
            # Coming into a place shows the player who is there. Hirelings travel with the
            # player character, who is the one whose sight matters.
            if actor.hireling:
                return
            for npc in state.characters.values():
                if npc.kind == NPC_KIND and npc.location == self.location:
                    _remember_npc(state, npc)
        elif actor.kind == NPC_KIND and actor.location == pc.location:
            # An NPC coming to the player's place is seen at once.
            _remember_npc(state, actor)

    def render(self) -> str:
        if self.location is None:
            return f"{self.name} went into the wind."
        return f"{self.name} moved to {self.location}."


@dataclass(frozen=True)
class NoteAdded(GameEvent):
    kind: ClassVar[str] = "note.added"
    hidden: ClassVar[bool] = True

    text: str
    # The name of the place or character the note is about, or None for the whole campaign.
    subject: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        subject = CAMPAIGN_WIDE if self.subject is None else self.subject
        state.notes.append(Note(id=event_id, subject=subject, text=self.text))

    def render(self) -> str:
        if self.subject is None:
            return f"Note: {self.text}"
        return f"Note about {self.subject}: {self.text}"


@dataclass(frozen=True)
class SettingRecorded(GameEvent):
    """One fact about the world the GM invented, keyed by topic. Recording a topic again replaces
    the old fact, so the GM can revise the setting as play makes it concrete."""

    kind: ClassVar[str] = "setting.recorded"
    hidden: ClassVar[bool] = True

    topic: str
    detail: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.settings[self.topic] = self.detail

    def render(self) -> str:
        return f"Setting, {self.topic}: {self.detail}"


@dataclass(frozen=True)
class WorldDescribed(GameEvent):
    """The player-facing overview of the world, written by the GM without secrets."""

    kind: ClassVar[str] = "world.described"
    hidden: ClassVar[bool] = True

    description: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.world_description = self.description

    def render(self) -> str:
        return "World overview written."


@dataclass(frozen=True)
class LocationUpdated(GameEvent):
    """A location's details after an update. The position is the resolved one, and None if the
    place has none. Discovery is not changed here; `LocationDiscovered` does that."""

    kind: ClassVar[str] = "location.updated"
    hidden: ClassVar[bool] = True

    name: str
    description: str
    x: float | None = None
    y: float | None = None
    icon: str | None = None
    # Old events have no icon or summary fields, so only a change touches them.
    icon_changed: bool = False
    summary: str | None = None
    summary_changed: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.places[key]
        state.places[key] = replace(
            before,
            name=self.name,
            description=self.description,
            position=_position(self.x, self.y),
            icon=self.icon if self.icon_changed else before.icon,
            summary=self.summary if self.summary_changed else before.summary,
        )

    def render(self) -> str:
        return f"Location updated: {self.name}."


@dataclass(frozen=True)
class LocationDiscovered(GameEvent):
    kind: ClassVar[str] = "location.discovered"

    name: str
    summary: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.places[key]
        # A place created with a player-facing summary keeps it when the move discovers it.
        state.places[key] = replace(
            before,
            discovered=True,
            summary=before.summary if self.summary is None else self.summary,
        )

    def render(self) -> str:
        return f"Discovered: {self.name}."


@dataclass(frozen=True)
class LocationSummarized(GameEvent):
    """A new player-facing summary for a place that is already discovered."""

    kind: ClassVar[str] = "location.summarized"
    hidden: ClassVar[bool] = True

    name: str
    summary: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        state.places[key] = replace(state.places[key], summary=self.summary)

    def render(self) -> str:
        return f"Summary updated: {self.name}."


@dataclass(frozen=True)
class LocationRemoved(GameEvent):
    kind: ClassVar[str] = "location.removed"
    hidden: ClassVar[bool] = True

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        del state.places[name_key(self.name)]

    def render(self) -> str:
        return f"Location removed: {self.name}."


@dataclass(frozen=True)
class CharacterSummarized(GameEvent):
    """The short player-facing summary of what the player knows of a character. It is separate
    from the GM's private description, so the player reads only what the GM chose to show."""

    kind: ClassVar[str] = "character.summarized"
    hidden: ClassVar[bool] = True

    name: str
    summary: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].summary = self.summary

    def render(self) -> str:
        return f"Character summary updated: {self.name}."


@dataclass(frozen=True)
class NpcUpdated(GameEvent):
    """An NPC's details after an update. The values are the resolved ones, not the changes."""

    kind: ClassVar[str] = "npc.updated"
    hidden: ClassVar[bool] = True

    name: str
    description: str
    personality: str
    accent: str
    quirks: list[str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        npc = state.characters[name_key(self.name)]
        npc.description = self.description
        npc.personality = self.personality
        npc.accent = self.accent
        npc.quirks = list(self.quirks)

    def render(self) -> str:
        return f"Character updated: {self.name}."


@dataclass(frozen=True)
class NpcKnown(GameEvent):
    """What the player character knows of an NPC: a snapshot of the details seen when the two met,
    and any relationship or last-known location the story has revealed since."""

    kind: ClassVar[str] = "npc.known"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    description: str = ""
    personality: str = ""
    accent: str = ""
    quirks: list[str] = field(default_factory=list)
    pronouns: str | None = None
    relationship: str = ""
    location: str | None = None
    # Older logs have no summary field, so its default keeps them decoding.
    summary: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.known[name_key(self.name)] = KnownNpc(
            name=self.name,
            description=self.description,
            summary=self.summary,
            personality=self.personality,
            accent=self.accent,
            quirks=tuple(self.quirks),
            pronouns=self.pronouns,
            relationship=self.relationship,
            location=self.location,
        )


@dataclass(frozen=True)
class NpcRemoved(GameEvent):
    kind: ClassVar[str] = "npc.removed"
    hidden: ClassVar[bool] = True

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        del state.characters[name_key(self.name)]
        # A removed NPC was only passing through, so the player no longer keeps them in mind.
        state.known.pop(name_key(self.name), None)

    def render(self) -> str:
        return f"Character removed: {self.name}."


@dataclass(frozen=True)
class NoteUpdated(GameEvent):
    kind: ClassVar[str] = "note.updated"
    hidden: ClassVar[bool] = True
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {"note_id": ("note.added",)}

    note_id: int
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.notes = [
            Note(id=note.id, subject=note.subject, text=self.text)
            if note.id == self.note_id
            else note
            for note in state.notes
        ]

    def render(self) -> str:
        return f"Note updated: {self.text}"


@dataclass(frozen=True)
class NoteRemoved(GameEvent):
    kind: ClassVar[str] = "note.removed"
    hidden: ClassVar[bool] = True
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {"note_id": ("note.added",)}

    note_id: int
    # Kept for the Log, which shows the note that was removed.
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.notes = [note for note in state.notes if note.id != self.note_id]

    def render(self) -> str:
        return f"Note removed: {self.text}"


@dataclass(frozen=True)
class StorylineCreated(GameEvent):
    """A plot thread begins. Its name and summary are player-facing; the rest is the GM's own
    record and may hold secrets. A storyline's id is the id of this event."""

    kind: ClassVar[str] = "storyline.created"
    hidden: ClassVar[bool] = True

    name: str
    summary: str
    plot: str = ""
    characters: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    notes: str = ""
    # Focus the new storyline even when another one already has the focus.
    focus: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        first_active = not state.active_storylines()
        state.storylines[event_id] = Storyline(
            id=event_id,
            name=self.name,
            summary=self.summary,
            plot=self.plot,
            characters=tuple(self.characters),
            locations=tuple(self.locations),
            objects=tuple(self.objects),
            notes=self.notes,
        )
        if self.focus or first_active:
            state.focus_storyline_id = event_id

    def render(self) -> str:
        return f"Storyline created: {self.name}."


@dataclass(frozen=True)
class StorylineUpdated(GameEvent):
    """A storyline's fields after an update. The values are the resolved ones, not the changes.
    Deactivating the focused storyline leaves no focus; the oldest active one takes over."""

    kind: ClassVar[str] = "storyline.updated"
    hidden: ClassVar[bool] = True
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {"storyline_id": ("storyline.created",)}

    storyline_id: int
    name: str
    summary: str
    plot: str
    characters: list[str]
    locations: list[str]
    objects: list[str]
    notes: str
    active: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        old = state.storylines.get(self.storyline_id)
        if old is None:
            return
        state.storylines[self.storyline_id] = Storyline(
            id=old.id,
            name=self.name,
            summary=self.summary,
            plot=self.plot,
            characters=tuple(self.characters),
            locations=tuple(self.locations),
            objects=tuple(self.objects),
            notes=self.notes,
            active=self.active,
        )
        if not self.active and state.focus_storyline_id == self.storyline_id:
            state.focus_storyline_id = None

    def render(self) -> str:
        return f"Storyline updated: {self.name}."


@dataclass(frozen=True)
class StorylineFocused(GameEvent):
    """The storyline the GM is now focused on, as the main plot of the moment."""

    kind: ClassVar[str] = "storyline.focused"
    hidden: ClassVar[bool] = True
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {"storyline_id": ("storyline.created",)}

    storyline_id: int
    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.focus_storyline_id = self.storyline_id

    def render(self) -> str:
        return f"Storyline focused: {self.name}."


@dataclass(frozen=True)
class JournalAppended(GameEvent):
    """Text added to the end of the journal. The journal is shown in its own panel, so the Log
    does not repeat it."""

    kind: ClassVar[str] = "journal.appended"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.journal = normalize_journal(f"{state.journal}\n{self.text}")


@dataclass(frozen=True)
class JournalReplaced(GameEvent):
    """One passage of the journal replaced by another, matched exactly once; an empty new passage
    deletes it."""

    kind: ClassVar[str] = "journal.replaced"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    old_text: str
    new_text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.journal = normalize_journal(state.journal.replace(self.old_text, self.new_text, 1))


@dataclass(frozen=True)
class JournalStarted(GameEvent):
    """A journal step began. The log it writes from is what was recorded after this event."""

    kind: ClassVar[str] = "journal.started"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.journal_through = event_id


@dataclass(frozen=True)
class NarrationUndone(GameEvent):
    """A turn review set the just-written narration aside and will write the story again.
    The narration itself was not recorded, so this only carries the review's reason, which the
    next narration reads so it does not repeat the fault."""

    kind: ClassVar[str] = "narration.undone"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    reason: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        _open_turn(state).revision = self.reason


@dataclass(frozen=True)
class ToolRan(GameEvent):
    """A rule-set tool finished during a turn. Its messages and context are what the model is
    sent next. `activity` is the Log text for the tool, if it has any."""

    kind: ClassVar[str] = "tool.ran"

    request_id: str
    messages: list[dict[str, Any]]
    context: list[dict[str, str]]
    activity: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        request = turn.queue.pop(0)
        if request.id != self.request_id:
            raise RuntimeError(f"tool {self.request_id!r} ran out of order")
        turn.messages.extend(self.messages)
        turn.context.extend(_parts(self.context))
        turn.prompt = None

    def render(self) -> str | None:
        return self.activity


@dataclass(frozen=True)
class SpeechWritten(GameEvent):
    """An NPC's line, written by a speech step during a tool run. The narration reads the lines
    in order. An empty `dialogue` is kept in the context only. A `replace` line takes the place of
    the speaker's queued line, if there is one."""

    kind: ClassVar[str] = "speech.written"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    speaker: str
    dialogue: str
    replace: bool
    context: list[dict[str, str]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.context.extend(_parts(self.context))
        if not self.dialogue:
            return
        line = {"speaker": self.speaker, "dialogue": self.dialogue}
        index = next(
            (
                i
                for i, queued in enumerate(turn.speech)
                if queued["speaker"].casefold() == self.speaker.casefold()
            ),
            None,
        )
        if self.replace and index is not None:
            turn.speech[index] = line
        else:
            turn.speech.append(line)


@dataclass(frozen=True)
class ChatPlayer(GameEvent):
    """The player's message. A message sent while no turn is open starts one; a message sent while
    a turn is open answers its prompt. `speaker` is the character the message speaks for, and
    `player_id` identifies the account at the table that wrote it. Older logs also stored a
    display name; it is ignored, and the current name is resolved from the account."""

    kind: ClassVar[str] = "chat.player"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str
    speaker: str | None = None
    player_id: int | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.turn is None:
            state.turn = Turn(
                opener=event_id,
                text=self.content,
                player_id=self.player_id,
            )

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class OpeningStarted(GameEvent):
    """The campaign's opening: the GM's first turn, which begins before the player acts. It is
    recorded with the player character, so the campaign is never left without its opening. A
    replacement character has its own opening, played by the player who made it."""

    kind: ClassVar[str] = "turn.opening"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    player_id: int | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.turn is None:
            state.turn = Turn(
                opener=event_id,
                text="",
                opening=True,
                player_id=self.player_id,
            )


@dataclass(frozen=True)
class ChatGM(GameEvent):
    """The GM's reply. The final reply closes the turn, and the turn's context is what this reply
    was written from. A reply that is not final is an out-of-character reply that the story
    continues after."""

    kind: ClassVar[str] = "chat.gm"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str
    model: str
    final: bool = True
    # Thinking models reject a later tool request that leaves this reply's reasoning out.
    reasoning: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        # A correction belongs to the narration it was written for. Once a reply is recorded,
        # a later narration must not be told to fix that stale fault.
        if state.turn is not None:
            state.turn.revision = ""
        if self.final:
            state.turn = None
        else:
            _open_turn(state).ooc = False

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class TurnFailed(GameEvent):
    """A turn step stopped with an error. The turn stays open so it can be continued."""

    kind: ClassVar[str] = "turn.failed"

    error: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.turn is not None:
            state.turn.failed = self.error

    def render(self) -> str:
        return f"The GM stopped: {self.error}."


@dataclass(frozen=True)
class SummaryUpdated(GameEvent):
    """The chat up to and including event `through` is now represented by `summary`."""

    kind: ClassVar[str] = "summary.updated"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {
        "through": ("chat.player", "chat.gm"),
    }

    summary: str
    through: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.summary = self.summary
        state.through = self.through


@dataclass(frozen=True)
class TurnBegun(GameEvent):
    """The model's context for the open turn, once it is set up."""

    kind: ClassVar[str] = "turn.begun"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    head: list[dict[str, str]]
    context: list[dict[str, str]]
    messages: list[dict[str, Any]]
    round_start: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.begun = True
        turn.head = _parts(self.head)
        turn.context = _parts(self.context)
        turn.messages = list(self.messages)
        turn.round_start = self.round_start


@dataclass(frozen=True)
class StateStepMade(GameEvent):
    """One state-management step of the open turn. With `requests`, they are queued to run.
    Without, the turn moves on to the narration, and any NPC dialogue is passed to it."""

    kind: ClassVar[str] = "turn.state_step"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    requests: list[dict[str, str]]
    messages: list[dict[str, Any]]
    context: list[dict[str, str]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.rounds += 1
        turn.messages.extend(self.messages)
        turn.context.extend(_parts(self.context))
        turn.queue = [ToolRequest(**request) for request in self.requests]
        if not self.requests:
            turn.narrating = True
            turn.speech = []


@dataclass(frozen=True)
class ReviewStepMade(GameEvent):
    """One turn review step's tool calls and results. The review reads the actions and narration
    just written. Its exchange stays in the turn's context for the rewrite, but is left out of the
    narration messages: the review is a check, not part of the story."""

    kind: ClassVar[str] = "turn.review_step"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    messages: list[dict[str, Any]]
    context: list[dict[str, str]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.context.extend(_parts(self.context))


@dataclass(frozen=True)
class OocStarted(GameEvent):
    """The open turn's player message is out of character. The out-of-character step replies
    next, and the story is narrated after that reply only when `narrate` is set."""

    kind: ClassVar[str] = "turn.ooc"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    narrate: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.ooc = True
        turn.ooc_narrate = self.narrate


@dataclass(frozen=True)
class PromptShown(GameEvent):
    """The open turn is waiting for the player to roll or type a reply. A prompt with `choices`
    is a move band's cost the player picks after the roll; `move`, `band`, `roll`, and `total`
    identify the roll it belongs to."""

    kind: ClassVar[str] = "turn.prompted"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    text: str
    dice: str
    details: str | None = None
    # Each choice is {"id", "label", "text"} from the move band the roll reached.
    choices: list[dict[str, str]] = field(default_factory=list)
    move: str = ""
    band: str = ""
    roll: int = 0
    total: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        _open_turn(state).prompt = Ask(
            self.text,
            self.dice,
            self.details,
            choices=tuple(self.choices),
            move=self.move,
            band=self.band,
            roll=self.roll,
            total=self.total,
        )


def _mark_used(mark: CellMark) -> bool:
    """Whether a cell mark sets anything. A mark with every field empty clears its cell."""
    return any((mark.terrain, mark.object, mark.color, mark.pattern, mark.icon, mark.label))


@dataclass(frozen=True)
class MapCreated(GameEvent):
    kind: ClassVar[str] = "map.created"
    hidden: ClassVar[bool] = True

    name: str
    location: str
    columns: int
    rows: int
    description: str
    background: str = ""
    palette: str = ""
    ambient: str = ""
    background_color: str = ""
    grid_color: str = ""
    # Each item is {"column", "row", "terrain", "object", "color", "pattern", "icon", "label"}.
    marks: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.maps[name_key(self.name)] = EncounterMap(
            name=self.name,
            location=self.location,
            columns=self.columns,
            rows=self.rows,
            description=self.description,
            background=self.background,
            marks={k: v for k, v in marks_from_list(self.marks).items() if _mark_used(v)},
            created=event_id,
            palette=self.palette,
            ambient=self.ambient,
            background_color=self.background_color,
            grid_color=self.grid_color,
        )

    def render(self) -> str:
        return (
            f"New encounter map: {self.name}, at {self.location} "
            f"({self.columns} by {self.rows} cells)."
        )


@dataclass(frozen=True)
class MapUpdated(GameEvent):
    """A map's size, description, name, or location changes. An empty `new_name` or `location`
    keeps the current one. An encounter running on the map follows it."""

    kind: ClassVar[str] = "map.updated"
    hidden: ClassVar[bool] = True

    name: str
    columns: int
    rows: int
    description: str
    new_name: str = ""
    location: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.maps.pop(key)
        name = self.new_name or before.name
        location = self.location or before.location
        # Marks and tokens outside a smaller map are dropped, so replaying the log gives the
        # same map.
        marks = {
            (column, row): mark
            for (column, row), mark in before.marks.items()
            if column < self.columns and row < self.rows
        }
        tokens = {
            key: cell
            for key, cell in before.tokens.items()
            if cell[0] < self.columns and cell[1] < self.rows
        }
        state.maps[name_key(name)] = EncounterMap(
            name=name,
            location=location,
            columns=self.columns,
            rows=self.rows,
            description=self.description,
            background=before.background,
            marks=marks,
            tokens=tokens,
            created=before.created,
            palette=before.palette,
            ambient=before.ambient,
            background_color=before.background_color,
            grid_color=before.grid_color,
        )
        encounter = state.encounter
        if encounter is not None and name_key(encounter.map) == key:
            encounter.map = name
            encounter.location = location

    def render(self) -> str:
        if self.new_name:
            return f"Encounter map updated: {self.name} is now {self.new_name}."
        return f"Encounter map updated: {self.name}."


@dataclass(frozen=True)
class MapStyleChanged(GameEvent):
    """A map's look changes: its background terrain, palette, ambient, and board colours. The
    service resolves every field, so apply sets them all rather than merging them."""

    kind: ClassVar[str] = "map.style_changed"
    hidden: ClassVar[bool] = True

    name: str
    background: str = ""
    background_color: str = ""
    grid_color: str = ""
    palette: str = ""
    ambient: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.maps.get(key)
        if before is None:
            return
        state.maps[key] = replace(
            before,
            background=self.background,
            background_color=self.background_color,
            grid_color=self.grid_color,
            palette=self.palette,
            ambient=self.ambient,
        )

    def render(self) -> str:
        return f"Encounter map restyled: {self.name}."


@dataclass(frozen=True)
class MapExtended(GameEvent):
    """A map grows on one or more sides. Every mark, token, and encounter cell shifts by the
    offset of the new top-left corner, so extending left or up keeps the existing scene."""

    kind: ClassVar[str] = "map.extended"
    hidden: ClassVar[bool] = True

    name: str
    columns: int
    rows: int
    offset_column: int = 0
    offset_row: int = 0

    def _shift(self, cell: tuple[int, int]) -> tuple[int, int]:
        return cell[0] + self.offset_column, cell[1] + self.offset_row

    def _inside(self, cell: tuple[int, int]) -> bool:
        return 0 <= cell[0] < self.columns and 0 <= cell[1] < self.rows

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.maps.get(key)
        if before is None:
            return
        marks = {
            shifted: mark
            for (column, row), mark in before.marks.items()
            if self._inside(shifted := self._shift((column, row)))
        }
        tokens = {
            name: shifted
            for name, cell in before.tokens.items()
            if self._inside(shifted := self._shift(cell))
        }
        state.maps[key] = replace(
            before, columns=self.columns, rows=self.rows, marks=marks, tokens=tokens
        )
        encounter = state.encounter
        if encounter is not None and name_key(encounter.map) == key:
            encounter.cells = {
                name: shifted
                for name, cell in encounter.cells.items()
                if self._inside(shifted := self._shift(cell))
            }

    def render(self) -> str:
        return f"Encounter map extended: {self.name} ({self.columns} by {self.rows} cells)."


@dataclass(frozen=True)
class MapCellsChanged(GameEvent):
    """Sets cells of a map. A cell whose terrain, object, colour, pattern, icon, and label are
    all "" is cleared."""

    kind: ClassVar[str] = "map.cells_changed"
    hidden: ClassVar[bool] = True

    name: str
    # Each item is {"column", "row", "terrain", "object", "color", "pattern", "icon", "label"}.
    marks: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter_map = state.maps[name_key(self.name)]
        for (column, row), mark in marks_from_list(self.marks).items():
            if _mark_used(mark):
                encounter_map.marks[(column, row)] = mark
            else:
                encounter_map.marks.pop((column, row), None)

    def render(self) -> str:
        return f"Encounter map cells changed: {self.name} ({len(self.marks)} cells)."


@dataclass(frozen=True)
class MapDeleted(GameEvent):
    kind: ClassVar[str] = "map.deleted"
    hidden: ClassVar[bool] = True

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.maps.pop(name_key(self.name), None)

    def render(self) -> str:
        return f"Encounter map deleted: {self.name}."


@dataclass(frozen=True)
class MapTokenPlaced(GameEvent):
    """A character, NPC, or prop is placed on a local map outside an encounter. When an encounter
    starts on the map, the character's token cell becomes their participant cell."""

    kind: ClassVar[str] = "map.token_placed"
    hidden: ClassVar[bool] = True

    map: str
    name: str
    column: int
    row: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter_map = state.maps.get(name_key(self.map))
        if encounter_map is not None:
            encounter_map.tokens[name_key(self.name)] = (self.column, self.row)

    def render(self) -> str:
        return f"{self.name} placed on {self.map} at column {self.column + 1}, row {self.row + 1}."


@dataclass(frozen=True)
class MapTokenRemoved(GameEvent):
    """A token is taken off a local map outside an encounter."""

    kind: ClassVar[str] = "map.token_removed"
    hidden: ClassVar[bool] = True

    map: str
    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter_map = state.maps.get(name_key(self.map))
        if encounter_map is not None:
            encounter_map.tokens.pop(name_key(self.name), None)

    def render(self) -> str:
        return f"{self.name} removed from {self.map}."


@dataclass(frozen=True)
class EncounterStarted(GameEvent):
    kind: ClassVar[str] = "encounter.started"

    map: str
    location: str
    rolls_initiative: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.encounter = Encounter(
            map=self.map, location=self.location, rolls_initiative=self.rolls_initiative
        )

    def render(self) -> str:
        return f"Encounter begins at {self.map}, {self.location}."


@dataclass(frozen=True)
class EncounterEnded(GameEvent):
    """The encounter is over. `reason` says why it ended early, such as a side being down."""

    kind: ClassVar[str] = "encounter.ended"

    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        for character in state.characters.values():
            character.fled = False
            character.surrendered = False
            character.critical_due = False
            if character.shield_suspended:
                character.armor = max(character.armor, character.shield_suspended_armor)
                character.shield_suspended = False
                character.shield_suspended_armor = 0
            if character.total_defense:
                character.armor = character.total_defense_armor
                character.total_defense = False
                character.total_defense_armor = 0
            character.grappling = ""
            character.grappled_by = ""
            if "grappled" in character.conditions:
                character.conditions.remove("grappled")
        state.encounter = None

    def render(self) -> str:
        if self.reason:
            return f"{self.reason} The encounter is over."
        return "The encounter is over."


@dataclass(frozen=True)
class MoraleChecked(GameEvent):
    """A side's morale save, made by its leader. A failed save makes the side flee.
    `tag` is "first" for the first casualty or "half" for half the side."""

    kind: ClassVar[str] = "morale.checked"

    side: str
    tag: str
    leader: str
    stat: str
    roll: int
    score: int
    passed: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.encounter is not None:
            state.encounter.morale.setdefault(self.side, []).append(self.tag)

    def render(self) -> str:
        label = MORALE_LABELS.get(self.tag, self.tag)
        result = "holds" if self.passed else "breaks"
        return (
            f"The {self.side} side checks morale at the {label}: {self.leader} rolls {self.roll} "
            f"on a {self.stat.upper()} save of {self.score} and the side {result}."
        )


@dataclass(frozen=True)
class CharacterFled(GameEvent):
    """A character has left the fight. `reason` is "morale" or "retreat"."""

    kind: ClassVar[str] = "character.fled"

    name: str
    reason: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].fled = True

    def render(self) -> str:
        if self.reason == "morale":
            return f"{self.name} breaks and flees."
        return f"{self.name} gets clear of the fight."


@dataclass(frozen=True)
class CharacterSurrendered(GameEvent):
    """A character has given up the fight and laid down arms."""

    kind: ClassVar[str] = "character.surrendered"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].surrendered = True

    def render(self) -> str:
        return f"{self.name} surrenders."


@dataclass(frozen=True)
class RetreatRolled(GameEvent):
    """A save to run from the fight. Unlike SaveRolled, a failure does not defeat anyone; the
    character just stays in the fight."""

    kind: ClassVar[str] = "retreat.rolled"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool

    def render(self) -> str:
        if self.passed:
            outcome = "and gets clear."
        else:
            outcome = "and cannot get clear."
        return (
            f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} {outcome}"
        )


@dataclass(frozen=True)
class ProjectStarted(GameEvent):
    """A project, research effort, magic working, or faction turn begins."""

    kind: ClassVar[str] = "project.started"

    name: str
    project_kind: str
    goal: str
    target: int
    cost: int = 0
    currency: str = ""
    owner: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.projects[name_key(self.name)] = Project(
            name=self.name,
            kind=self.project_kind,
            goal=self.goal,
            target=self.target,
            cost=self.cost,
            currency=self.currency,
            owner=self.owner,
        )

    def render(self) -> str:
        return f"{self.name} begins ({self.project_kind}): {self.goal}, {self.target} points."


@dataclass(frozen=True)
class ProjectAdvanced(GameEvent):
    """Progress on a project. A note records what the time, money, or roll bought."""

    kind: ClassVar[str] = "project.advanced"

    name: str
    amount: int
    note: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        project = state.projects.get(name_key(self.name))
        if project is None:
            return
        project.progress = min(project.target, project.progress + self.amount)
        if self.note:
            project.notes.append(self.note)
        if project.progress >= project.target:
            project.completed = True

    def render(self) -> str:
        text = f"{self.name} advances by {self.amount}"
        return f"{text} ({self.note})." if self.note else f"{text}."


@dataclass(frozen=True)
class ProjectAbandoned(GameEvent):
    kind: ClassVar[str] = "project.abandoned"

    name: str
    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        project = state.projects.get(name_key(self.name))
        if project is not None:
            project.abandoned = True

    def render(self) -> str:
        text = f"{self.name} is abandoned"
        return f"{text} ({self.reason})." if self.reason else f"{text}."


@dataclass(frozen=True)
class Recovered(GameEvent):
    """A character recovers harm from one stat, by healing, a rest, paid treatment, or a rule-set
    effect. `amount` is the harm actually restored, never more than the character had taken.
    `cause` names what restored it. `restores` lists the stats a rest set out to restore, so the
    Log can say the recovery came from a rest rather than from one named cause."""

    kind: ClassVar[str] = "character.recovered_harm"

    name: str
    stat: str
    amount: int
    cause: str = ""
    restores: list[str] = field(default_factory=list)

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        taken = character.harm.get(self.stat, 0)
        remaining = taken - max(0, min(self.amount, taken))
        if remaining:
            character.harm[self.stat] = remaining
        else:
            character.harm.pop(self.stat, None)

    def render(self) -> str:
        text = f"{self.name} recovers {self.amount} {self.stat.upper()}"
        if self.cause:
            return f"{text} ({self.cause})."
        if self.restores:
            return f"{text} from the rest."
        return f"{text}."


@dataclass(frozen=True)
class CharacterRecovered(GameEvent):
    """A player character brought back from a dying condition. The GM says how they survived and
    what it cost them."""

    kind: ClassVar[str] = "character.recovered"

    name: str
    explanation: str
    consequence: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.defeated = False
        character.dying_hours = None
        character.dying_rounds = None

    def render(self) -> str:
        return f"{self.name} comes to. {self.explanation} {self.consequence}"


@dataclass(frozen=True)
class ParticipantJoined(GameEvent):
    kind: ClassVar[str] = "encounter.joined"

    name: str
    side: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.order.append(state.characters[name_key(self.name)].name)
        encounter.sides[name_key(self.name)] = self.side
        # In round one the order is set by initiative, so a late joiner waits at the end of it.
        if encounter.round > 1:
            group_by_side(encounter)

    def render(self) -> str:
        return f"{self.name} joins the {self.side} side of the encounter."


@dataclass(frozen=True)
class ParticipantLeft(GameEvent):
    kind: ClassVar[str] = "encounter.left"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        key = name_key(self.name)
        index = next(i for i, name in enumerate(encounter.order) if name_key(name) == key)
        del encounter.order[index]
        encounter.cells.pop(key, None)
        encounter.sides.pop(key, None)
        encounter.initiative.pop(key, None)
        if encounter.acting is not None and name_key(encounter.acting) == key:
            encounter.acting = None
            encounter.moved = 0
        if index < encounter.up:
            encounter.up -= 1
        if encounter.order and encounter.up >= len(encounter.order):
            _next_round(encounter)

    def render(self) -> str:
        return f"{self.name} leaves the encounter."


@dataclass(frozen=True)
class ParticipantPlaced(GameEvent):
    """A participant is put on a cell. `cost` is the cells moved, which only counts against the
    movement allowance when the participant is taking their turn; placing them otherwise is free."""

    kind: ClassVar[str] = "encounter.placed"

    name: str
    column: int
    row: int
    cost: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        key = name_key(self.name)
        encounter.cells[key] = (self.column, self.row)
        if encounter.acting is not None and name_key(encounter.acting) == key:
            encounter.moved += self.cost

    def render(self) -> str:
        return f"{self.name} moves to column {self.column + 1}, row {self.row + 1}."


@dataclass(frozen=True)
class EncounterTurnBegun(GameEvent):
    """A participant's turn starts. An NPC's turn opens a GM reply that plays it out, from the
    instruction recorded here. A player character's turn opens nothing: the player acts in chat
    and ends the turn with a button."""

    kind: ClassVar[str] = "encounter.turn_begun"

    name: str
    instruction: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        character = state.characters[name_key(self.name)]
        if character.shield_suspended:
            character.armor = max(character.armor, character.shield_suspended_armor)
            character.shield_suspended = False
            character.shield_suspended_armor = 0
        if character.total_defense:
            character.armor = character.total_defense_armor
            character.total_defense = False
            character.total_defense_armor = 0
        encounter.acting = character.name
        encounter.moved = 0
        if character.kind == NPC_KIND and state.turn is None:
            state.turn = Turn(opener=event_id, text=self.instruction, actor=character.name)

    def render(self) -> str:
        return f"{self.name}'s turn begins."


@dataclass(frozen=True)
class EncounterTurnEnded(GameEvent):
    kind: ClassVar[str] = "encounter.turn_ended"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        character = state.characters.get(name_key(self.name))
        if character is not None and character.surprised_turns > 0:
            character.surprised_turns -= 1
        encounter.acting = None
        encounter.moved = 0
        encounter.up += 1
        if encounter.up >= len(encounter.order):
            _next_round(encounter)
        _skip_defeated(encounter, state)

    def render(self) -> str:
        return f"{self.name}'s turn ends."


@dataclass(frozen=True)
class InitiativeRolled(GameEvent):
    """A player character's initiative save. Passing acts before the opponents and failing
    acts after them."""

    kind: ClassVar[str] = "encounter.initiative_rolled"

    name: str
    passed: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.initiative[name_key(self.name)] = self.passed

    def render(self) -> str:
        result = "passes" if self.passed else "fails"
        return f"{self.name} {result} the initiative save."


@dataclass(frozen=True)
class InitiativeSettled(GameEvent):
    """The round one order, once every player character has rolled for initiative."""

    kind: ClassVar[str] = "encounter.initiative_settled"

    order: list[str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.order = list(self.order)
        encounter.up = 0
        encounter.settled = True
        # Settling can reorder the sides after a player character already joined and had a turn
        # opened, so clear that provisional turn and let the settled order open the right one.
        encounter.acting = None
        encounter.moved = 0

    def render(self) -> str:
        return "Initiative is set: " + ", ".join(self.order) + "."


@dataclass(frozen=True)
class ShieldBashed(GameEvent):
    """A character bashed with a shield. Its AC bonus is suspended until the character's next
    turn, when EncounterTurnBegun restores it."""

    kind: ClassVar[str] = "combat.shield_bashed"
    hidden: ClassVar[bool] = True

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if character.shield_suspended or not character.equipped_shield_value:
            return
        character.shield_suspended_armor = character.armor
        character.shield_suspended = True
        character.armor = max(0, character.armor - character.shield_bonus)

    def render(self) -> str:
        return f"{self.name} bashes with a shield; its AC bonus is suspended until the next turn."


@dataclass(frozen=True)
class FeatureChoiceMade(GameEvent):
    """A structured choice for a feature, such as the attribute for Developed Attribute."""

    kind: ClassVar[str] = "character.feature_choice_made"

    name: str
    feature: str
    pick: str
    choice: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        key = f"{self.feature}:{self.pick}"
        character.feature_choices.setdefault(key, []).append(self.choice)

    def render(self) -> str:
        return f"{self.name} chooses {self.choice} for {self.feature} ({self.pick})."


@dataclass(frozen=True)
class ExecutionPrepared(GameEvent):
    """A character spent the preparation time for an execution attack with `weapon`."""

    kind: ClassVar[str] = "combat.execution_prepared"
    hidden: ClassVar[bool] = True

    name: str
    weapon: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.execution_prepared = True
        character.execution_weapon = self.weapon

    def render(self) -> str:
        return f"{self.name} prepares an execution attack with {self.weapon}."


@dataclass(frozen=True)
class ExecutionTaken(GameEvent):
    """An execution attack was made, clearing the preparation."""

    kind: ClassVar[str] = "combat.execution_taken"
    hidden: ClassVar[bool] = True

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.execution_prepared = False
        character.execution_weapon = ""

    def render(self) -> str:
        return f"{self.name} makes an execution attack."


@dataclass(frozen=True)
class GrappleStarted(GameEvent):
    """One character grapples another. Both carry the grappled condition, and both are linked
    until GrappleEnded."""

    kind: ClassVar[str] = "combat.grapple_started"

    holder: str
    target: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        holder = state.characters[name_key(self.holder)]
        target = state.characters[name_key(self.target)]
        holder.grappling = target.name
        target.grappled_by = holder.name
        for character in (holder, target):
            if "grappled" not in character.conditions:
                character.conditions.append("grappled")

    def render(self) -> str:
        return f"{self.holder} grapples {self.target}."


@dataclass(frozen=True)
class GrappleEnded(GameEvent):
    """A grapple ends, freeing both characters and removing the grappled condition."""

    kind: ClassVar[str] = "combat.grapple_ended"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        other_name = character.grappling or character.grappled_by
        other = state.characters.get(name_key(other_name)) if other_name else None
        character.grappling = ""
        character.grappled_by = ""
        if "grappled" in character.conditions:
            character.conditions.remove("grappled")
        if other is not None:
            other.grappling = ""
            other.grappled_by = ""
            if "grappled" in other.conditions:
                other.conditions.remove("grappled")

    def render(self) -> str:
        return f"{self.name} breaks free of the grapple."


@dataclass(frozen=True)
class Surprised(GameEvent):
    """A character was caught off guard. They lose `turns` turns, consumed as each of their
    encounter turns ends."""

    kind: ClassVar[str] = "combat.surprised"
    hidden: ClassVar[bool] = True

    name: str
    turns: int = 1

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.surprised_turns = max(character.surprised_turns, self.turns)

    def render(self) -> str:
        return f"{self.name} is surprised and loses {self.turns} turn(s)."


@dataclass(frozen=True)
class TotalDefenseTaken(GameEvent):
    """A character spent a Main action on total defense. `bonus` is added to the armor total until
    the character's next turn, when EncounterTurnBegun restores the armor before the action."""

    kind: ClassVar[str] = "combat.total_defense_taken"
    hidden: ClassVar[bool] = True

    name: str
    bonus: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if character.total_defense or self.bonus <= 0:
            return
        character.total_defense_armor = character.armor
        character.total_defense = True
        character.armor += self.bonus

    def render(self) -> str:
        return f"{self.name} takes total defense; armor rises by {self.bonus} until the next turn."


@dataclass(frozen=True)
class Struck(GameEvent):
    """A hit on a character after armor. `harm` is what each stat loses: hit points take what they
    can, and any overflow comes off the stat named by `save`, which the target must then save
    against. Under the "roll_vs_defense" hit mode a blow that fails to reach the target's defense
    is a miss: `hit` is false, `to_hit` is the attack roll, and no harm is done, unless the rule
    set's shock does harm on a miss, which `shock` is. `defeats` is set when the harm took the
    stat that decides defeat to 0; `status` is where that leaves the character."""

    kind: ClassVar[str] = "combat.struck"

    name: str
    attacker: str
    weapon: str
    raw: int
    armor: int
    harm: dict[str, int]
    save: str | None = None
    modifier: str = ""
    rolled: str = ""
    hit: bool = True
    to_hit: int | None = None
    # The shock a miss still did, which is part of `harm` above.
    shock: int = 0
    # The encounter round in which a shield turned this blow's Shock aside.
    shield_round: int = 0
    defeats: bool = False
    status: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for stat, amount in self.harm.items():
            character.harm[stat] = character.harm.get(stat, 0) + amount
        if self.save is not None:
            character.save_due = self.save
        if self.shield_round:
            character.shock_blocked_round = self.shield_round
        if self.defeats:
            character.defeated = True

    def render(self) -> str:
        taken = sum(self.harm.values())
        weapon = f"{self.weapon} ({self.modifier})" if self.modifier else self.weapon
        if not self.hit:
            text = f"{self.attacker} swings the {weapon} at {self.name} and misses"
            if self.to_hit is not None:
                text += f" (rolled {self.to_hit})"
            text += "."
            if self.shield_round:
                text += " A shield turns the shock aside."
            if self.shock:
                text += f" The shock still does {taken} harm"
                stats = ", ".join(stat.upper() for stat in self.harm)
                if stats:
                    text += f" to {stats}"
                text += "."
            if self.defeats:
                text += f" {self.name} is {self.status}."
            return text
        text = f"{self.attacker} strikes {self.name} with the {weapon} for {self.raw}"
        if self.armor:
            text += f", {self.armor} absorbed by armor"
        text += f"; {self.name} takes {taken} harm."
        if self.rolled:
            text += f" {self.rolled}"
        if self.save is not None:
            text += f" A {self.save.upper()} save is due."
        if self.defeats:
            text += f" {self.name} is {self.status}."
        return text


@dataclass(frozen=True)
class SaveRolled(GameEvent):
    """A save against a stat. A failed save defeats the character, leaving it `status`. A
    failed critical save on a large group leaves it to the GM's decision instead."""

    kind: ClassVar[str] = "combat.save_rolled"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool
    status: str = ""
    critical: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if character.save_due == self.stat:
            character.save_due = None
        if not self.passed:
            if self.critical:
                character.critical_due = True
            else:
                character.defeated = True

    def render(self) -> str:
        result = "succeeds" if self.passed else "fails"
        text = f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} and {result}."
        if not self.passed and self.critical:
            text += f" {self.name} is broken, and the GM decides whether it is defeated or flees."
        elif not self.passed:
            text += f" {self.name} is {self.status}."
        return text


@dataclass(frozen=True)
class GroupBreakResolved(GameEvent):
    """The GM's decision after a large group's critical save failed. `outcome` is
    "defeat", leaving it `status`, or "flee", breaking it up so its members run from the fight."""

    kind: ClassVar[str] = "combat.group_break_resolved"

    name: str
    outcome: str
    status: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.critical_due = False
        if self.outcome == "defeat":
            character.defeated = True
        else:
            character.fled = True

    def render(self) -> str:
        if self.outcome == "defeat":
            return f"{self.name} is {self.status}."
        return f"{self.name} breaks up, and its members flee the fight."


@dataclass(frozen=True)
class TablesRolled(GameEvent):
    """The GM rolled on one or more dice tables. Each roll records its table, name, roll, and
    result."""

    kind: ClassVar[str] = "dice.tables_rolled"
    hidden: ClassVar[bool] = True

    rolls: list[dict[str, Any]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return " ".join(
            f"{roll['name']}: rolls {roll['roll']} and gets {roll['result']}."
            for roll in self.rolls
        )


@dataclass(frozen=True)
class DungeonEventRolled(GameEvent):
    """A roll on the rule set's dungeon event table. It does not move the day or hour clock."""

    kind: ClassVar[str] = "dungeon.event"
    hidden: ClassVar[bool] = True

    roll: int
    event: str
    text: str
    triggers: list[str]
    reason: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"Dungeon event ({self.reason}): rolls {self.roll}, {self.event}. {self.text}"


@dataclass(frozen=True)
class ReactionRolled(GameEvent):
    kind: ClassVar[str] = "reaction.rolled"
    hidden: ClassVar[bool] = True

    roll: int
    band: str
    reason: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"Reaction ({self.reason}): rolls {self.roll}, {self.band}."


def _why(reason: str) -> str:
    return f" ({reason})" if reason else ""


@dataclass(frozen=True)
class WeatherRolled(GameEvent):
    """A roll on the weather table of the season. `weather` is the catastrophe result when a run
    of catastrophe weather reached the streak; `catastrophe` says so, and `extreme` says the roll
    itself landed on the catastrophe weather."""

    kind: ClassVar[str] = "wilderness.weather_rolled"
    hidden: ClassVar[bool] = True

    day: int
    season: str
    roll: int
    weather: str
    catastrophe: bool = False
    extreme: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.weather_day = self.day
        # A Catastrophic day ends the run, so the next Extreme starts a new one.
        if self.catastrophe or not self.extreme:
            state.weather_streak = 0
        else:
            state.weather_streak += 1

    def render(self) -> str:
        return f"Weather (day {self.day}, {self.season}): rolls {self.roll}, {self.weather}."


@dataclass(frozen=True)
class SeasonSet(GameEvent):
    """The season the campaign starts in. The wilderness calendar counts its days from here."""

    kind: ClassVar[str] = "wilderness.season_set"
    hidden: ClassVar[bool] = True

    season: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.start_season = self.season

    def render(self) -> str:
        return f"The campaign starts in {self.season}."


@dataclass(frozen=True)
class WildernessEventRolled(GameEvent):
    """A roll on the Wilderness Events table during a Wilderness Action. It does not move the clock."""

    kind: ClassVar[str] = "wilderness.event_rolled"
    hidden: ClassVar[bool] = True

    action: str
    roll: int
    event: str
    text: str
    night: bool
    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        night = " at night" if self.night else ""
        return (
            f"Wilderness event ({self.action}{night}{_why(self.reason)}): "
            f"rolls {self.roll}, {self.event}. {self.text}"
        )


# Kept so logs written by earlier supply and camp actions still replay. New watches log WildernessEffectApplied.
@dataclass(frozen=True)
class SupplyGathered(GameEvent):
    """Rations found by a Supply action. The die that sets the find has `sides` faces."""

    kind: ClassVar[str] = "wilderness.supply_gathered"
    hidden: ClassVar[bool] = True

    rations: int
    sides: int
    party_size: int
    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"Supplies{_why(self.reason)}: found {self.rations} ration(s) on a d{self.sides}."


@dataclass(frozen=True)
class CampMade(GameEvent):
    kind: ClassVar[str] = "wilderness.camp_made"
    hidden: ClassVar[bool] = True

    party_size: int
    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"Camp made{_why(self.reason)} for a party of {self.party_size}."


@dataclass(frozen=True)
class WildernessEffectApplied(GameEvent):
    """One declarative effect from the rule set's wilderness hooks, applied to the party.
    `do` names the kind of effect and `text` says what it did."""

    kind: ClassVar[str] = "wilderness.effect_applied"
    hidden: ClassVar[bool] = True

    action: str
    do: str
    text: str
    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"Wilderness effect ({self.action}{_why(self.reason)}): {self.text}"


@dataclass(frozen=True)
class LostRolled(GameEvent):
    """A lost check on a path. `roll` is None when the path can never lose the party."""

    kind: ClassVar[str] = "wilderness.lost_rolled"
    hidden: ClassVar[bool] = True

    path: str
    roll: int | None
    lost: bool
    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        if self.roll is None:
            return f"Path {self.path}{_why(self.reason)}: never lost."
        verdict = "lost" if self.lost else "not lost"
        return f"Path {self.path}{_why(self.reason)}: rolls {self.roll}, {verdict}."


@dataclass(frozen=True)
class ArmorSet(GameEvent):
    """A character's armor total changed. The character sheet and inventory tab already show the
    total, so the transcript does not announce it; the log line stays available as a hidden
    event."""

    kind: ClassVar[str] = "combat.armor_set"
    hidden: ClassVar[bool] = True

    name: str
    armor: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].armor = self.armor

    def render(self) -> str:
        return f"{self.name}'s armor is now {self.armor}."


@dataclass(frozen=True)
class DefenseEquipmentSet(GameEvent):
    """The armor and shield a character has equipped by explicit choice. Internal, so it adds no
    Log line of its own. `armor` is not changed here; ArmorSet still sets the armor total."""

    kind: ClassVar[str] = "combat.defense_equipment_set"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    equipped_armor: str = ""
    equipped_armor_value: int = 0
    equipped_shield: str = ""
    equipped_shield_value: int = 0
    shield_bonus: int = 0
    innate_defense: int = 0
    defense_adjustment: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.equipped_armor = self.equipped_armor
        character.equipped_armor_value = self.equipped_armor_value
        character.equipped_shield = self.equipped_shield
        character.equipped_shield_value = self.equipped_shield_value
        character.shield_bonus = self.shield_bonus
        character.innate_defense = self.innate_defense
        character.defense_adjustment = self.defense_adjustment
        character.defense_tracked = True


@dataclass(frozen=True)
class InvincibilitySet(GameEvent):
    """A debug buff on a player character. While it holds, no harm reaches the character. Turning
    it on cancels a save the character owes, since no harm can make it due."""

    kind: ClassVar[str] = "combat.invincibility_set"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    active: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.invincible = self.active
        if self.active:
            character.save_due = None

    def render(self) -> str:
        return f"{self.name} is {'invincible' if self.active else 'no longer invincible'}."


@dataclass(frozen=True)
class DebugStatSet(GameEvent):
    """A debug setting of a player character's current score for one stat, by recording the harm
    that leaves it at `remaining`. Internal, so the GM does not see it."""

    kind: ClassVar[str] = "combat.debug_stat_set"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    stat: str
    remaining: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.harm[self.stat] = max(0, character.stats.get(self.stat, 0) - self.remaining)

    def render(self) -> str:
        return f"{self.name}'s {self.stat.upper()} is set to {self.remaining}."


@dataclass(frozen=True)
class DebugCurrencyAdded(GameEvent):
    """A debug grant of the rule set's currency, for testing purchases before the game has a way to
    earn it. Internal, so the GM does not see it."""

    kind: ClassVar[str] = "character.debug_currency_added"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    feature: str
    amount: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.features[self.feature] = character.features.get(self.feature, 0) + self.amount

    def render(self) -> str:
        return f"{self.name} is given {self.amount} {self.feature} from the debug panel."


@dataclass(frozen=True)
class DebugInjectionSet(GameEvent):
    """A one-shot fault the debug panel forces into the next GM turn: a tool call run just before
    the narration, or text that replaces it."""

    kind: ClassVar[str] = "debug.injection_set"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    mode: str
    tool: str = ""
    arguments: str = "{}"
    text: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.debug_injection = DebugInjection(
            mode=self.mode, tool=self.tool, arguments=self.arguments, text=self.text
        )


@dataclass(frozen=True)
class DebugInjectionCleared(GameEvent):
    """The pending debug injection was taken by a turn, or removed from the debug panel."""

    kind: ClassVar[str] = "debug.injection_cleared"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.debug_injection = None


@dataclass(frozen=True)
class DebugInjectionRan(GameEvent):
    """A debug-injected tool call and its result, added to the open turn so the narration and the
    review see the wrong action exactly as if the GM had made it."""

    kind: ClassVar[str] = "debug.injection_ran"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    tool: str
    messages: list[dict[str, Any]]
    context: list[dict[str, str]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.messages.extend(self.messages)
        turn.context.extend(_parts(self.context))


@dataclass(frozen=True)
class EffectNoted(GameEvent):
    """A rule effect with no mechanical change, such as a location roll or a choice the GM narrates."""

    kind: ClassVar[str] = "effect.noted"
    hidden: ClassVar[bool] = True

    name: str
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"{self.name}: {self.text}"


@dataclass(frozen=True)
class ReactionUsed(GameEvent):
    """A feature reaction changed a roll. The use counts against the reaction's limit until its
    reset label clears it."""

    kind: ClassVar[str] = "character.reaction_used"

    name: str
    feature: str
    feature_name: str
    reaction: str
    reset: str
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        key = f"{self.reset}:{self.feature}:{self.reaction}"
        character.reaction_uses[key] = character.reaction_uses.get(key, 0) + 1

    def render(self) -> str:
        return f"{self.name} uses {self.feature_name}: {self.text}"


@dataclass(frozen=True)
class ReactionsReset(GameEvent):
    """The limited reactions carrying a reset label are available again."""

    kind: ClassVar[str] = "character.reactions_reset"
    hidden: ClassVar[bool] = True

    name: str
    reset: str
    label: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        prefix = f"{self.reset}:"
        character.reaction_uses = {
            key: value
            for key, value in character.reaction_uses.items()
            if not key.startswith(prefix)
        }

    def render(self) -> str:
        return f"{self.name}: {self.label or self.reset} reactions reset."


@dataclass(frozen=True)
class MaxChanged(GameEvent):
    """A stat's maximum score changed from `before` to `after`. `cause` says what changed it."""

    kind: ClassVar[str] = "character.max_changed"

    name: str
    stat: str
    before: int
    after: int
    cause: str = ""
    # What to call the value in the log: "maximum" for a stat with a separate current score,
    # empty for a plain score.
    label: str = "maximum"

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].stats[self.stat] = self.after

    def render(self) -> str:
        prefix = f"{self.label} " if self.label else ""
        text = f"{self.name}'s {prefix}{self.stat.upper()} goes from {self.before} to {self.after}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ConditionAdded(GameEvent):
    kind: ClassVar[str] = "condition.added"

    name: str
    condition: str
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].conditions.append(self.condition)

    def render(self) -> str:
        text = f"{self.name} gains the condition {self.condition}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ConditionEnded(GameEvent):
    kind: ClassVar[str] = "condition.ended"

    name: str
    condition: str
    how: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        conditions = state.characters[name_key(self.name)].conditions
        if self.condition in conditions:
            conditions.remove(self.condition)

    def render(self) -> str:
        return f"{self.name} no longer has the condition {self.condition} (ended by {self.how})."


@dataclass(frozen=True)
class CounterChanged(GameEvent):
    """A counter a character keeps moved, such as Experience marked or Luck spent. `before` and `after`
    are its values, after the counter's bounds were applied."""

    kind: ClassVar[str] = "counter.changed"

    name: str
    counter: str
    label: str
    before: int
    after: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].counters[self.counter] = self.after

    def render(self) -> str:
        text = f"{self.name}'s {self.label} goes from {self.before} to {self.after}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class AdvancementTaken(GameEvent):
    """A character spent an Advancement on an option. `stat` is the stat the option raised, or ""."""

    kind: ClassVar[str] = "character.advancement_taken"

    name: str
    option: str
    label: str
    stat: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        taken = state.characters[name_key(self.name)].advancements_taken
        taken[self.option] = taken.get(self.option, 0) + 1

    def render(self) -> str:
        return f"{self.name} takes the Advancement {self.label}" + (
            f" ({self.stat.upper()})." if self.stat else "."
        )


@dataclass(frozen=True)
class RelationChanged(GameEvent):
    """A character gained or lost a relation with someone, such as a Friend or a Rival."""

    kind: ClassVar[str] = "character.relation_changed"

    name: str
    relation: str
    label: str
    other: str
    added: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        held = state.characters[name_key(self.name)].relations.setdefault(self.relation, [])
        if self.added:
            held.append(self.other)
        else:
            held.remove(self.other)

    def render(self) -> str:
        if self.added:
            return f"{self.name} makes {self.other} their {self.label}."
        return f"{self.name} is no longer {self.other}'s {self.label}."


@dataclass(frozen=True)
class TallyChanged(GameEvent):
    """A campaign tally moved for one entry, such as a House's points. `before` and `after` are its
    values for the entry."""

    kind: ClassVar[str] = "tally.changed"

    tally: str
    label: str
    entry: str
    entry_label: str
    before: int
    after: int
    cause: str = ""
    # The key that makes this award unique within its tally, or empty for no key.
    award: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.tallies.setdefault(self.tally, {})[self.entry] = self.after
        if self.award:
            state.tally_awards.setdefault(self.tally, set()).add(self.award)
        if self.after > self.before:
            gains = state.tally_gains.setdefault(self.tally, {}).setdefault(self.entry, set())
            gains.add(self.after - self.before)

    def render(self) -> str:
        text = f"{self.entry_label} {self.label} goes from {self.before} to {self.after}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class TallyReset(GameEvent):
    """A tally starts over at the end of a period. `final` holds each entry's total when it ended,
    by entry id, and `labels` names the entries."""

    kind: ClassVar[str] = "tally.reset"

    tally: str
    label: str
    final: dict[str, int]
    labels: dict[str, str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.tallies[self.tally] = {}
        state.tally_awards.pop(self.tally, None)
        state.tally_gains.pop(self.tally, None)

    def render(self) -> str:
        totals = ", ".join(f"{self.labels[e]} {v}" for e, v in self.final.items())
        return f"{self.label} resets. Final totals: {totals}."


@dataclass(frozen=True)
class FactionAdded(GameEvent):
    """A faction the GM invented, such as a magic-school college. Its points start in `tally`."""

    kind: ClassVar[str] = "faction.added"

    faction_id: str
    name: str
    description: str = ""
    trait: str = ""
    tally: str = ""
    entry: str = ""
    points: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.factions[self.faction_id] = Faction(
            id=self.faction_id,
            name=self.name,
            description=self.description,
            trait=self.trait,
            tally=self.tally,
            entry=self.entry,
        )
        if self.tally and self.entry:
            state.tallies.setdefault(self.tally, {})[self.entry] = self.points

    def render(self) -> str:
        trait = f" ({self.trait})" if self.trait else ""
        if not self.tally or not self.entry:
            return f"New faction: {self.name}{trait}."
        return f"New faction: {self.name}{trait}, {self.points} points."


@dataclass(frozen=True)
class FactionUpdated(GameEvent):
    """A faction's name, description, or trait changed. Its id and tally entry do not."""

    kind: ClassVar[str] = "faction.updated"

    faction_id: str
    name: str
    description: str = ""
    trait: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is not None:
            faction.name = self.name
            faction.description = self.description
            faction.trait = self.trait

    def render(self) -> str:
        return f"Faction {self.name} is updated."


@dataclass(frozen=True)
class FactionRemoved(GameEvent):
    """A faction disbands and its cup entry is removed."""

    kind: ClassVar[str] = "faction.removed"

    faction_id: str
    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.pop(self.faction_id, None)
        if faction is not None:
            state.tallies.get(faction.tally, {}).pop(faction.entry, None)
            state.tally_gains.get(faction.tally, {}).pop(faction.entry, None)

    def render(self) -> str:
        return f"The faction {self.name} disbands."


@dataclass(frozen=True)
class FactionAssetAdded(GameEvent):
    """A faction buys an asset. Its stats are resolved from the rule set's definition now, so a
    later content change does not alter the campaign's record of what it owns."""

    kind: ClassVar[str] = "faction.asset_added"

    faction_id: str
    asset_id: str
    asset_kind: str
    name: str
    category: str
    hp_max: int
    hp: int
    magic: str
    attack: str = ""
    counter: str = ""
    qualities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is None:
            return
        faction.assets[self.asset_id] = FactionAsset(
            id=self.asset_id,
            kind=self.asset_kind,
            name=self.name,
            category=self.category,
            hp_max=self.hp_max,
            hp=max(0, min(self.hp, self.hp_max)),
            magic=self.magic,
            attack=self.attack,
            counter=self.counter,
            qualities=list(self.qualities),
            notes=list(self.notes),
        )

    def render(self) -> str:
        return f"New faction asset: {self.name} ({self.category})."


@dataclass(frozen=True)
class FactionAssetUpdated(GameEvent):
    """A faction asset's name, hit points, notes, or qualities changed. The event stores the full
    resolved values, so replay does not depend on the earlier event's contents."""

    kind: ClassVar[str] = "faction.asset_updated"

    faction_id: str
    asset_id: str
    name: str
    hp: int
    notes: list[str] = field(default_factory=list)
    qualities: list[str] = field(default_factory=list)

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is None:
            return
        asset = faction.assets.get(self.asset_id)
        if asset is None:
            return
        asset.name = self.name
        asset.hp = max(0, min(self.hp, asset.hp_max))
        asset.notes = list(self.notes)
        asset.qualities = list(self.qualities)

    def render(self) -> str:
        return f"Faction asset {self.name} is updated."


@dataclass(frozen=True)
class FactionAssetRemoved(GameEvent):
    """A faction asset is destroyed or disbanded."""

    kind: ClassVar[str] = "faction.asset_removed"

    faction_id: str
    asset_id: str
    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is not None:
            faction.assets.pop(self.asset_id, None)

    def render(self) -> str:
        return f"The faction asset {self.name} is lost."


@dataclass(frozen=True)
class FactionAssetActed(GameEvent):
    """A faction asset used its action for the current faction turn. It cannot act again until
    the faction takes another turn. The Factions tab shows the readiness, so the transcript does
    not announce the bookkeeping."""

    kind: ClassVar[str] = "faction.asset_acted"
    hidden: ClassVar[bool] = True

    faction_id: str
    asset_id: str
    name: str = ""
    note: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is None:
            return
        asset = faction.assets.get(self.asset_id)
        if asset is None:
            return
        asset.acted_turn = faction.turn
        if self.note:
            asset.notes.append(self.note)

    def render(self) -> str:
        text = f"{self.name or 'A faction asset'} acts this faction turn"
        return f"{text} ({self.note})." if self.note else f"{text}."


@dataclass(frozen=True)
class FactionAssetDamaged(GameEvent):
    """A faction asset lost hit points, never below zero."""

    kind: ClassVar[str] = "faction.asset_damaged"

    faction_id: str
    asset_id: str
    amount: int
    name: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is None:
            return
        asset = faction.assets.get(self.asset_id)
        if asset is not None:
            asset.hp = max(0, asset.hp - self.amount)

    def render(self) -> str:
        return f"{self.name or 'A faction asset'} takes {self.amount} damage."


@dataclass(frozen=True)
class FactionAssetRepaired(GameEvent):
    """A faction asset regained hit points, never above its maximum."""

    kind: ClassVar[str] = "faction.asset_repaired"

    faction_id: str
    asset_id: str
    amount: int
    name: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is None:
            return
        asset = faction.assets.get(self.asset_id)
        if asset is not None:
            asset.hp = min(asset.hp_max, asset.hp + self.amount)

    def render(self) -> str:
        return f"{self.name or 'A faction asset'} is repaired for {self.amount} hit points."


@dataclass(frozen=True)
class FactionTurnTaken(GameEvent):
    """A faction began its next turn. Every asset that acted in an earlier turn is ready again.
    The Factions tab shows the turn, so the transcript does not announce the bookkeeping."""

    kind: ClassVar[str] = "faction.turn_taken"
    hidden: ClassVar[bool] = True

    faction_id: str
    turn: int
    name: str = ""
    note: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        faction = state.factions.get(self.faction_id)
        if faction is not None:
            faction.turn = self.turn

    def render(self) -> str:
        text = f"{self.name or 'The faction'} begins faction turn {self.turn}"
        return f"{text} ({self.note})." if self.note else f"{text}."


@dataclass(frozen=True)
class FeatureLearned(GameEvent):
    """A character learns a feature, such as a spell, that the rule set's Advancements teach."""

    kind: ClassVar[str] = "character.feature_learned"

    name: str
    feature: str
    feature_name: str
    cause: str = ""
    repeat: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.features[self.feature] = (
            character.features.get(self.feature, 0) + 1 if self.repeat else 1
        )

    def render(self) -> str:
        text = f"{self.name} learns {self.feature_name}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class CustomFeatureCreated(GameEvent):
    """A character invents a feature, such as a researched spell, potion, or enchantment. The
    feature is kept on the character and learned at once, so it needs no matching rule-set
    definition."""

    kind: ClassVar[str] = "character.custom_feature_created"

    name: str
    feature_id: str
    feature_name: str
    feature_type: str
    description: str
    tag: str = ""
    rank: int = 0
    subject: str = ""
    base: str = ""
    mods: list[str] = field(default_factory=list)
    stress: int = 0
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        feature = CustomFeature(
            id=self.feature_id,
            name=self.feature_name,
            feature_type=self.feature_type,
            description=self.description,
            tag=self.tag,
            rank=self.rank,
            subject=self.subject,
            base=self.base,
            mods=list(self.mods),
            stress=self.stress,
            cause=self.cause,
        )
        character.custom_features[feature.id] = feature
        character.features[feature.id] = 1

    def render(self) -> str:
        text = f"{self.name} invents {self.feature_name}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class CustomFeatureUpdated(GameEvent):
    """A character's invented feature changes. Only the fields given are replaced."""

    kind: ClassVar[str] = "character.custom_feature_updated"

    name: str
    feature_id: str
    feature_name: str | None = None
    description: str | None = None
    tag: str | None = None
    rank: int | None = None
    subject: str | None = None
    base: str | None = None
    mods: list[str] | None = None
    stress: int | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        feature = character.custom_features.get(self.feature_id)
        if feature is None:
            return
        for key in (
            "feature_name",
            "description",
            "tag",
            "rank",
            "subject",
            "base",
            "mods",
            "stress",
        ):
            value = getattr(self, key)
            if value is not None:
                setattr(feature, "name" if key == "feature_name" else key, value)

    def render(self) -> str:
        name = self.feature_name or self.feature_id
        return f"{self.name} refines {name}."


@dataclass(frozen=True)
class CustomFeatureRemoved(GameEvent):
    """A character's invented feature is given up, lost, or refuted."""

    kind: ClassVar[str] = "character.custom_feature_removed"

    name: str
    feature_id: str
    feature_name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.custom_features.pop(self.feature_id, None)
        character.features.pop(self.feature_id, None)

    def render(self) -> str:
        return f"{self.name} loses {self.feature_name}."


@dataclass(frozen=True)
class StartingItemsTransferred(GameEvent):
    """Move starting item features out of the sheet before adding their inventory items."""

    kind: ClassVar[str] = "character.starting_items_transferred"
    hidden: ClassVar[bool] = True
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    features: list[str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for feature in self.features:
            del character.features[feature]


@dataclass(frozen=True)
class PeriodEnded(GameEvent):
    """A stretch of play, such as a school year, ended for every character at once."""

    kind: ClassVar[str] = "period.ended"

    period: str
    label: str

    def render(self) -> str:
        return f"The {self.label} ends."


@dataclass(frozen=True)
class InjuryGained(GameEvent):
    """An injury from the Injuries table. `roll` is the table entry it came from."""

    kind: ClassVar[str] = "injury.gained"

    name: str
    injury: str
    roll: int
    text: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].injuries.append(self.injury)

    def render(self) -> str:
        text = f"{self.name} gains the injury {self.injury} (Injuries table {self.roll})."
        return f"{text} {self.text}" if self.text else text


@dataclass(frozen=True)
class Harmed(GameEvent):
    """Harm taken outside combat, which takes no armor. `defeats` is set when it took the stat that
    decides defeat to 0; `status` is where that leaves the character."""

    kind: ClassVar[str] = "combat.harmed"

    name: str
    stat: str
    taken: int
    cause: str = ""
    defeats: bool = False
    status: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if self.taken:
            character.harm[self.stat] = character.harm.get(self.stat, 0) + self.taken
        if self.defeats:
            character.defeated = True

    def render(self) -> str:
        text = f"{self.name} loses {self.taken} {self.stat.upper()}"
        if self.cause:
            text += f" from {self.cause}"
        text += "."
        if self.defeats:
            text += f" {self.name} is {self.status}."
        return text


@dataclass(frozen=True)
class CharacterSaved(GameEvent):
    """A save made outside a fight, such as one an injury calls for. Unlike SaveRolled, a failure does
    not defeat the character; the effects of a pass or fail are recorded as their own events."""

    kind: ClassVar[str] = "character.saved"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        result = "succeeds" if self.passed else "fails"
        text = f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} and {result}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class MoveMade(GameEvent):
    """A move rolled and landed in one of its bands. The band's effects are recorded as their own
    events, after this one."""

    kind: ClassVar[str] = "move.made"

    name: str
    move: str
    roll: int
    bonus: int
    total: int
    band: str
    text: str = ""
    cause: str = ""
    # The stat the roll added, or "" when the move rolled none.
    stat: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        sign = f"{self.bonus:+d}" if self.bonus else ""
        text = (
            f"{self.name} makes {self.move}: rolls {self.roll}{sign} for {self.total}, {self.band}"
        )
        if self.text:
            text = f"{text}. {self.text}"
        if self.cause:
            text = f"{text} ({self.cause})"
        return text if text.endswith((".", "!", "?")) else f"{text}."


@dataclass(frozen=True)
class MoveChoiceMade(GameEvent):
    """The player picked one of the costs a move band offered. The choice's effects are recorded
    as their own events, after this one."""

    kind: ClassVar[str] = "move.choice_made"

    name: str
    move: str
    band: str
    choice: str
    label: str = ""
    text: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        chosen = self.label or self.choice
        text = f"{self.name} chooses {chosen} for {self.move} ({self.band})"
        if self.text:
            text = f"{text}. {self.text}"
        return text if text.endswith((".", "!", "?")) else f"{text}."


@dataclass(frozen=True)
class CheckMade(GameEvent):
    """A check rolled outside a move, such as a skill check. `check` is the check's label, and
    `skill`/`skill_name`/`skill_level` identify the skill and its level. `skill_bonus` is what the
    skill added, and `stat`/`stat_name` identify the stat. `bonus` is the whole bonus the roll got,
    and `success` is whether it reached the difficulty."""

    kind: ClassVar[str] = "check.made"

    name: str
    check: str
    skill: str = ""
    skill_name: str = ""
    skill_level: int = 0
    skill_bonus: int | None = None
    stat: str = ""
    stat_name: str = ""
    roll: int = 0
    bonus: int = 0
    total: int = 0
    difficulty: int = 0
    success: bool = False
    cause: str = ""
    # What worn armor took off the roll, or 0 when nothing did.
    armor_penalty: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        sign = f"{self.bonus:+d}" if self.bonus else ""
        used = []
        if self.skill_name:
            skill_bonus = self.skill_level if self.skill_bonus is None else self.skill_bonus
            used.append(f"{self.skill_name} {skill_bonus:+d}")
        if self.stat_name:
            used.append(self.stat_name)
        if self.armor_penalty:
            used.append(f"armor {self.armor_penalty:+d}")
        with_used = f" ({', '.join(used)})" if used else ""
        result = "succeeds" if self.success else "fails"
        text = (
            f"{self.name} makes a {self.check or 'check'}{with_used}: rolls {self.roll}{sign} "
            f"for {self.total} against difficulty {self.difficulty} and {result}"
        )
        if self.cause:
            text = f"{text} ({self.cause})"
        return f"{text}."


@dataclass(frozen=True)
class SkillChanged(GameEvent):
    """A character's level in one skill changed from `before` to `after`."""

    kind: ClassVar[str] = "character.skill_changed"

    name: str
    skill: str
    skill_name: str
    before: int
    after: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].skills[self.skill] = self.after

    def render(self) -> str:
        if self.before == self.after:
            text = f"{self.name} becomes trained in {self.skill_name} at level {self.after}"
        else:
            text = f"{self.name}'s {self.skill_name} goes from {self.before} to {self.after}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


# The WWN ruleset modules still build this event directly. They are outside this cleanup's file
# ownership, so keep the old name as an alias for `Recovered` until they call the recovery service.
StatRestored = Recovered


@dataclass(frozen=True)
class ClockAdvanced(GameEvent):
    """The campaign clock moved to a new day and part. A player character in a place is there
    at the new time, so the place counts as visited."""

    kind: ClassVar[str] = "clock.advanced"

    day: int
    part: int
    part_name: str
    hour: int = 0
    # The hours this move passed, which count down a dying character's clock.
    elapsed_hours: int = 0
    new_day: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.clock = Clock(day=self.day, part=self.part, hour=self.hour)
        for character in state.characters.values():
            if character.dying_hours is not None:
                character.dying_hours = max(0, character.dying_hours - self.elapsed_hours)
        for character in state.characters.values():
            if character.kind != PC_KIND or character.location is None:
                continue
            key = name_key(character.location)
            if key in state.places:
                state.places[key] = replace(state.places[key], visited_day=self.day)

    def render(self) -> str:
        return f"It is day {self.day}, {self.part_name}."


@dataclass(frozen=True)
class TravelCharged(GameEvent):
    """Travel minutes were charged to the clock. The minutes left over from a whole hour carry to
    the next move."""

    kind: ClassVar[str] = "travel.charged"
    channel: ClassVar[Channel] = Channel.INTERNAL

    minutes: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.travel_minutes = self.minutes


@dataclass(frozen=True)
class CharacterDying(GameEvent):
    """A player character is out of action and bleeding out. It dies when `hours` pass unless it
    is stabilized, or sooner when the rule set gives a `rounds` clock and turns pass."""

    kind: ClassVar[str] = "character.dying"

    name: str
    hours: int
    rounds: int | None = None
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.dying_hours = self.hours
        character.dying_rounds = self.rounds

    def render(self) -> str:
        source = f" {self.cause}" if self.cause else ""
        if self.rounds is not None:
            rounds = f"{self.rounds} round{'' if self.rounds == 1 else 's'}"
            return f"{self.name} is dying{source}. Death comes within {rounds} unless aided."
        hours = f"{self.hours} hour{'' if self.hours == 1 else 's'}"
        return f"{self.name} is dying{source}. Death comes within {hours} unless aided."


@dataclass(frozen=True)
class DyingRoundsPassed(GameEvent):
    """A round passed while a character was dying. At zero rounds the character dies."""

    kind: ClassVar[str] = "character.dying_rounds"

    name: str
    rounds: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].dying_rounds = self.rounds

    def render(self) -> str:
        return f"{self.name} has {self.rounds} round{'' if self.rounds == 1 else 's'} left."


@dataclass(frozen=True)
class CharacterStabilized(GameEvent):
    """Aid stopped a dying character's clock. It is still out of action until the GM recovers
    it."""

    kind: ClassVar[str] = "character.stabilized"

    name: str
    by: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.dying_hours = None
        character.dying_rounds = None

    def render(self) -> str:
        source = f" {self.by}." if self.by else ""
        return f"{self.name} is stabilized.{source}"


@dataclass(frozen=True)
class CharacterDied(GameEvent):
    """A character died. The body stays where it fell, with the gear it carried."""

    kind: ClassVar[str] = "character.died"

    name: str
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.dead = True
        character.defeated = True
        character.dying_hours = None
        character.dying_rounds = None

    def render(self) -> str:
        return f"{self.name} dies{f', {self.cause}' if self.cause else ''}."


@dataclass(frozen=True)
class CharacterRetired(GameEvent):
    """A character left the story. It is no longer played, and the player creates a new one."""

    kind: ClassVar[str] = "character.retired"

    name: str
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].retired = True

    def render(self) -> str:
        return f"{self.name} leaves the story{f' ({self.cause})' if self.cause else ''}."


@dataclass(frozen=True)
class RestTaken(GameEvent):
    kind: ClassVar[str] = "rest.taken"

    length: str
    full: bool
    interrupted: bool
    reason: str
    day: int
    part: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.rests.append(
            RestRecord(
                day=self.day,
                part=self.part,
                length=self.length,
                full=self.full,
                interrupted=self.interrupted,
                reason=self.reason,
            )
        )

    def render(self) -> str:
        interrupted = ", interrupted" if self.interrupted else ""
        return f"The party rests ({self.length}{interrupted}): {self.reason.rstrip('.!? ')}."


@dataclass(frozen=True)
class CurrencyChanged(GameEvent):
    """Money changes hands. `feature` is the rule set's currency, held as a feature count. The
    balance never goes below zero."""

    kind: ClassVar[str] = "character.currency_changed"

    name: str
    feature: str
    amount: int
    cause: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        balance = character.features.get(self.feature, 0) + self.amount
        character.features[self.feature] = max(0, balance)

    def render(self) -> str:
        if self.amount < 0:
            return f"{self.name} pays {-self.amount} for {self.cause}."
        return f"{self.name} gains {self.amount} from {self.cause}."


@dataclass(frozen=True)
class MarkSet(GameEvent):
    """A rule set's hook sets or clears a mark on a character. `value` is any JSON value, and None
    clears the key. `text` is what the event says."""

    kind: ClassVar[str] = "character.mark_set"

    name: str
    key: str
    value: Any
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        marks = state.characters[name_key(self.name)].marks
        if self.value is None:
            marks.pop(self.key, None)
        else:
            marks[self.key] = self.value

    def render(self) -> str:
        return self.text


@dataclass(frozen=True)
class ItemAdded(GameEvent):
    kind: ClassVar[str] = "character.item_added"

    name: str
    item: str
    size: str
    slots: int
    kind_of_item: str = ""
    cause: str = ""
    text: str = ""
    uses: int = 0
    max_uses: int = 0
    damage: str = ""
    rules: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    ready: bool = True

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.items.append(
            Item(
                name=self.item,
                size=self.size,
                slots=self.slots,
                kind=self.kind_of_item,
                text=self.text,
                damage=self.damage,
                uses=self.uses,
                max_uses=self.max_uses,
                rules=tuple(self.rules),
                flags=tuple(self.flags),
                ready=self.ready,
            )
        )

    def render(self) -> str:
        text = f"{self.name} gains {self.item}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ObjectPlaced(GameEvent):
    """An object is put in a place. A hidden one is kept out of the log's text, so players do not
    read of it before it is found."""

    kind: ClassVar[str] = "object.placed"

    place: str
    contents: str
    currency: int = 0
    item: str = ""
    item_size: str = ""
    item_slots: int = 0
    hidden: bool = False
    cause: str = ""
    name: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.objects[event_id] = WorldObject(
            id=event_id,
            place=self.place,
            contents=self.contents,
            currency=self.currency,
            item=self.item,
            item_size=self.item_size,
            item_slots=self.item_slots,
            hidden=self.hidden,
            name=self.name,
        )

    def render(self) -> str | None:
        if self.hidden:
            return None
        text = self.contents.rstrip()
        if not text.endswith((".", "!", "?")):
            text += "."
        return f"{text} It is in {self.place}."


@dataclass(frozen=True)
class ObjectTaken(GameEvent):
    """A character takes an object from its place. The currency and item it held are recorded by
    their own events."""

    kind: ClassVar[str] = "object.taken"
    id_refs: ClassVar[dict[str, tuple[str, ...]]] = {"object_id": ("object.placed",)}

    object_id: int
    name: str
    contents: str
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.objects.pop(self.object_id, None)

    def render(self) -> str:
        text = f"{self.name} takes {self.contents}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ItemUsesSet(GameEvent):
    """Sets a charged item's uses left, after a use or a recharge. `cause` says which."""

    kind: ClassVar[str] = "character.item_uses_set"

    name: str
    item: str
    uses: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for index, held in enumerate(character.items):
            if name_key(held.name) == name_key(self.item):
                spent_on = state.clock if self.cause == "used" else None
                character.items[index] = replace(held, uses=self.uses, spent_on=spent_on)
                return

    def render(self) -> str:
        text = f"{self.name}'s {self.item} has {self.uses} use{'' if self.uses == 1 else 's'} left"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class SpellCast(GameEvent):
    """A spell cast from a source the rule set names, such as a spellbook or a scroll. The cost, if
    any, is recorded as its own event."""

    kind: ClassVar[str] = "magic.spell_cast"

    name: str
    spell: str
    source: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"{self.name} casts {self.spell} from a {self.source}."


@dataclass(frozen=True)
class ItemReadied(GameEvent):
    """An item is readied or stowed. A stowed item cannot be used until it is readied again."""

    kind: ClassVar[str] = "character.item_readied"

    name: str
    item: str
    ready: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for index, held in enumerate(character.items):
            if name_key(held.name) == name_key(self.item):
                character.items[index] = replace(held, ready=self.ready)
                return

    def render(self) -> str:
        return f"{self.name} {'readies' if self.ready else 'stows'} {self.item}."


@dataclass(frozen=True)
class ItemRemoved(GameEvent):
    """Removes the first item of the character with this name, and of this kind if one is given."""

    kind: ClassVar[str] = "character.item_removed"

    name: str
    item: str
    kind_of_item: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for index, held in enumerate(character.items):
            if name_key(held.name) != name_key(self.item):
                continue
            if self.kind_of_item and held.kind != self.kind_of_item:
                continue
            del character.items[index]
            return

    def render(self) -> str:
        return f"{self.name} loses {self.item}."


@dataclass(frozen=True)
class CharacterDrained(GameEvent):
    """Harm takes a stat to 0 outside combat, such as when an over-full inventory drains a stat. It is not
    overflow, so no save is owed."""

    kind: ClassVar[str] = "character.drained"

    name: str
    stat: str
    cause: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.harm[self.stat] = character.stats.get(self.stat, 0)

    def render(self) -> str:
        return f"{self.name}'s {self.stat.upper()} drops to 0 ({self.cause})."


def is_hidden(kind: str) -> bool:
    """Whether the player's transcript hides events of this kind. An unknown kind raises KeyError."""
    return EVENT_TYPES[kind].hidden
