"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import get_close_matches
from typing import Any, Literal, TypeVar

from sqlalchemy import Engine
from sqlmodel import Session

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterError,
    CharacterSpec,
    Feature,
    Sheet,
    TableOption,
    campaign_schema,
    check_choices,
    check_swaps,
    choice_options,
    resolve,
)
from cygnusforge.content.library import (
    AdvancementRules,
    CheckRules,
    ClockRules,
    CombatRules,
    ConditionRules,
    CounterRules,
    DiceTable,
    EncounterGrid,
    EncounterObject,
    EncounterTemplate,
    EncounterTerrain,
    Hooks,
    InventoryRules,
    MagicRules,
    Marketplace,
    MonsterTemplate,
    MoveRules,
    PeriodRules,
    RelationRules,
    RestLength,
    ShopItem,
    TallyRules,
    Travel,
    Wilderness,
    WorldTemplate,
    WorldTerrain,
)
from cygnusforge.rules.context import brief
from cygnusforge.rules.dice import is_formula
from cygnusforge.services import advancements as advancements_service
from cygnusforge.services import aid, combat, encounters, morale
from cygnusforge.services import attacks as attack_modifiers
from cygnusforge.services import checks as checks_service
from cygnusforge.services import clock as clock_service
from cygnusforge.services import counters as counters_service
from cygnusforge.services import inventory as inventory_service
from cygnusforge.services import magic as magic_service
from cygnusforge.services import market as market_service
from cygnusforge.services import moves as moves_service
from cygnusforge.services import objects as objects_service
from cygnusforge.services import periods as periods_service
from cygnusforge.services import relations as relations_service
from cygnusforge.services import rests as rests_service
from cygnusforge.services import tallies as tallies_service
from cygnusforge.services import wilderness as wilderness_service
from cygnusforge.services.attacks import ATTACK_FLAGS
from cygnusforge.services.common import (
    InvalidInput,
    bounded_text,
    session_scope,
    whole_number,
)
from cygnusforge.services.conditions import add_condition, end_condition
from cygnusforge.services.dice_tables import roll_creative as roll_creative_tables
from cygnusforge.services.dice_tables import roll_tables as roll_dice_tables
from cygnusforge.services.dungeon import dungeon_cycle as dungeon_cycle_service
from cygnusforge.services.dungeon import dungeon_rules
from cygnusforge.services.dungeon import roll_reaction as roll_reaction_service
from cygnusforge.services.events import (
    CAMPAIGN_WIDE,
    NPC_KIND,
    PC_KIND,
    ActorMoved,
    ArmorSet,
    CampaignOptionsChosen,
    CampaignState,
    Character,
    HirelingAdded,
    HirelingDismissed,
    HirelingPromoted,
    Item,
    JournalAppended,
    JournalReplaced,
    KnownNpc,
    LocationAdded,
    LocationDiscovered,
    LocationRemoved,
    LocationSummarized,
    LocationUpdated,
    MaxChanged,
    Note,
    NoteAdded,
    NoteRemoved,
    NoteUpdated,
    NpcAdded,
    NpcKnown,
    NpcRemoved,
    NpcStatted,
    NpcUpdated,
    OpeningStarted,
    PcAdded,
    Place,
    ProjectAbandoned,
    ProjectAdvanced,
    ProjectStarted,
    Recovered,
    RegionAdded,
    SeasonSet,
    SkillChanged,
    StartingItemsTransferred,
    TravelCharged,
    current,
    load_state,
    name_key,
    record,
    same_item_name,
)
from cygnusforge.services.geometry import clearance, overlaps, polygon_problem, uncovered
from cygnusforge.services.hooks import HookContext, call_hook
from cygnusforge.services.recovery import recover
from cygnusforge.services.tools import check_arguments
from cygnusforge.services.views import (
    _character_named,
    _children_of,
    _flag_text,
    _location_entry,
    _npc_summary,
    _npc_words,
    _npcs_at,
    _place_named,
    get_statblock,
    map_view,
    present_rules_text,
    scene_view,
    speaker_profile,
    time_view,
    world_state_text,
)

__all__ = [
    "get_statblock",
    "map_view",
    "present_rules_text",
    "scene_view",
    "speaker_profile",
    "time_view",
    "world_state_text",
]

T = TypeVar("T")
MAX_NAME = 120
MAX_STAT = 999
MAX_TEXT = 2000
# A player-facing summary of a place: a sentence or two, not the place's full description.
MAX_SUMMARY = 300
MAX_QUIRKS = 10
QUIRKS_PER_NPC = 2
# A region outline may have at most this many corners.
MAX_REGION_CORNERS = 200
# A place given a map point must lie this far inside a region's edge, so the ground around it shows.
REGION_BUFFER_KM = 0.5
# Two places count as sharing a point when their positions agree to this many kilometers.
SAME_POINT_KM = 0.01


# How a player character moves: overland takes wilderness time, in a settlement takes minutes at a
# speed, and in a dungeon takes no clock time.
OVERLAND = "overland"
IN_SETTLEMENT = "in_settlement"
IN_DUNGEON = "in_dungeon"
MOVE_MODES = (OVERLAND, IN_SETTLEMENT, IN_DUNGEON)


@dataclass(frozen=True)
class WorldTools:
    """The world as the rule set's tools use it. Each call runs in its own session and commits."""

    engine: Engine
    campaign_id: int
    tables: Mapping[str, Sequence[TableOption]]
    rng: random.Random
    character: CharacterSpec
    # The rule set's structured JSON data, indexed by definition id.
    rule_data: Mapping[str, object] = field(default_factory=dict, kw_only=True)
    # The rule set's encounter grid, or None when its encounters have no movement limit.
    grid: EncounterGrid | None = None
    # The rule set's encounter terrain, objects, and map templates by id.
    terrain: Mapping[str, EncounterTerrain] = field(default_factory=dict)
    encounter_objects: Mapping[str, EncounterObject] = field(default_factory=dict)
    encounter_templates: Mapping[str, EncounterTemplate] = field(default_factory=dict)
    # The rule set's combat, or None when it has none, and its monster statblocks by id.
    combat: CombatRules | None = None
    monsters: Mapping[str, MonsterTemplate] = field(default_factory=dict)
    # The world's own features by id. give_item finds them by name.
    features: Mapping[str, Feature] = field(default_factory=dict)
    # The rule set's checks by id, such as its skill check.
    checks: Mapping[str, CheckRules] = field(default_factory=dict)
    # The rule set's conditions by id.
    conditions: Mapping[str, ConditionRules] = field(default_factory=dict)
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)
    # The rule set's moves by id.
    moves: Mapping[str, MoveRules] = field(default_factory=dict)
    # The rule set's counters by id, such as Experience or Luck.
    counters: Mapping[str, CounterRules] = field(default_factory=dict)
    advancements: Mapping[str, AdvancementRules] = field(default_factory=dict)
    periods: Mapping[str, PeriodRules] = field(default_factory=dict)
    tallies: Mapping[str, TallyRules] = field(default_factory=dict)
    relations: Mapping[str, RelationRules] = field(default_factory=dict)
    # Time, rest, and the rule set's hooks. Each is None or empty when the rule set does not use it.
    clock_rules: ClockRules | None = None
    rests: Mapping[str, RestLength] = field(default_factory=dict)
    interrupted_rest: str = ""
    hooks: Hooks | None = None
    inventory: InventoryRules | None = None
    item_sizes: Mapping[str, int] = field(default_factory=dict)
    magic: MagicRules | None = None
    marketplace: Marketplace | None = None
    # What "+" between dice terms means in this rule set: "sum" or "highest".
    plus: str = field(kw_only=True)
    # The rule set's HP tiers for NPCs without a statblock, by tier name.
    npc_hp: Mapping[str, int] = field(default_factory=dict)
    npc_hp_stat: str | None = None
    # The rule set's travel modes and default leg, or None when it does not charge travel time.
    travel: Travel | None = None
    # The rule set's wilderness calendar, weather, and Wilderness Actions, or None when it has none.
    wilderness: Wilderness | None = None
    # The kinds of ground a world map's regions can be, by id.
    world_terrain: Mapping[str, WorldTerrain] = field(default_factory=dict)

    def create_region(
        self,
        name: str,
        terrain: str,
        points: Sequence[Sequence[float]],
        force: bool = False,
        discovered: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_region(
                db,
                self.campaign_id,
                self.world_terrain,
                name,
                terrain,
                points,
                force=force,
                discovered=discovered,
            )

    def roll_tables(self, tables: list[str]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return roll_dice_tables(
                db, self.campaign_id, tables, self.dice_tables, self.rng, plus=self.plus
            )

    def draw_character_table(self, table: str) -> str:
        """Choose one option from a character table, without recording a campaign event."""
        options = self.tables.get(table)
        if not options:
            raise InvalidInput(f"there is no character table named {table!r}")
        return self.rng.choice(options).value

    def roll_creative(self, tables: list[str] | None = None) -> dict[str, Any]:
        """Roll the creative tables. Without `tables`, every creative table is rolled."""
        with session_scope(self.engine) as db:
            return roll_creative_tables(
                db, self.campaign_id, tables, self.dice_tables, self.rng, plus=self.plus
            )

    def advance_time(self, days: int = 0, parts: int = 0, hours: int = 0) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return clock_service.advance_time(
                db,
                self.campaign_id,
                _required(self.clock_rules, "time"),
                self.hooks,
                self.inventory,
                days,
                parts,
                hours,
            )

    def rest(self, length: str, interrupted: bool = False, reason: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return rests_service.take_rest(
                db,
                self.campaign_id,
                _required(self.clock_rules, "time"),
                self.rests,
                self.interrupted_rest,
                self.hooks,
                self.inventory,
                length,
                interrupted,
                reason,
                combat=self.combat,
                conditions=self.conditions,
                rng=self.rng,
                plus=self.plus,
                spec=self.character,
            )

    def rest_history(self) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return rests_service.rest_history(db, self.campaign_id)

    def with_hook_context(self, action: Callable[[HookContext], T]) -> T:
        with session_scope(self.engine) as db:
            result = action(HookContext(db, self.campaign_id, self.hooks, self.inventory))
            db.commit()
            return result

    def add_penalty(self, name: str, cause: str, force: bool = False) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            if not force and self.hooks is not None:
                hc = HookContext(db, self.campaign_id, self.hooks, self.inventory)
                character = inventory_service.character_named(hc.state(), name)
                reason = call_hook(self.hooks, "held_back", hc, character.name, default="")
                if reason:
                    penalty = _required(self.inventory, "inventory").penalty_name
                    raise InvalidInput(
                        f"{character.name} is held back by {reason}, so set force only if "
                        f"something besides that causes this {penalty}"
                    )
            return inventory_service.add_penalty(
                db, self.campaign_id, _required(self.inventory, "inventory"), name, cause
            )

    def buy_item(
        self, name: str, item: str, price: int | None = None, size: str = ""
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.buy_item(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                item,
                price,
                size,
                armor_max=self.combat.armor_max if self.combat else 0,
                combat=self.combat,
                character=self.character,
            )

    def change_currency(self, name: str, amount: int, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.change_currency(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                name,
                amount,
                cause,
            )

    def place_object(
        self,
        place: str,
        contents: str,
        hidden: bool,
        cause: str,
        currency: int | None = None,
        item: str | None = None,
        item_size: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return objects_service.place_object(
                db,
                self.campaign_id,
                self.marketplace,
                self.item_sizes,
                place,
                contents,
                hidden,
                cause,
                currency,
                item,
                item_size,
                name,
            )

    def take_object(self, object_name: str, character: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return objects_service.take_object(
                db,
                self.campaign_id,
                self.marketplace,
                self.inventory,
                object_name,
                character,
                cause,
            )

    def buy_service(self, names: Sequence[str], service: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.buy_service(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                names,
                service,
                self.hooks,
            )

    def _give_catalogue_item(self, db: Session, name: str, item: str) -> bool:
        listed = self._listed(item)
        if listed is None:
            raise InvalidInput(f"this rule set has no item named {item!r} to give")
        rules = _required(self.inventory, "inventory")
        if not inventory_service.fits_listed(
            db, self.campaign_id, rules, self.item_sizes, name, listed
        ):
            return False
        inventory_service.carry_listed(db, self.campaign_id, rules, self.item_sizes, name, listed)
        return True

    def _buy_catalogue_item(self, db: Session, name: str, item: str) -> dict[str, Any]:
        """Buy one copy of a catalogue item for a character, at its listed price."""
        return market_service.buy_item(
            db,
            self.campaign_id,
            _required(self.marketplace, "a marketplace"),
            _required(self.inventory, "inventory"),
            self.item_sizes,
            name,
            item,
            armor_max=self.combat.armor_max if self.combat else 0,
            combat=self.combat,
            character=self.character,
        )

    def _listed(self, item: str, *, include_character: bool = False) -> ShopItem | None:
        if self.marketplace is not None:
            listed = market_service.listed_item(self.marketplace, item)
            if listed is not None:
                return listed
        relic = next((f for f in self.features.values() if same_item_name(f.name, item)), None)
        if relic is None and include_character:
            relic = next(
                (
                    f
                    for f in self.character.features.values()
                    if f.feature_type == "item" and same_item_name(f.name, item)
                ),
                None,
            )
        if relic is None:
            return None
        return ShopItem(
            id=relic.id,
            name=relic.name,
            category=relic.feature_type,
            price=0,
            size=relic.size,
            armor=relic.armor,
            damage=relic.damage,
            uses=relic.uses,
            description=relic.description,
            armor_mode=(
                "shield"
                if "shield" in relic.flags
                else "max"
                if relic.armor and self.combat is not None and self.combat.defense_base
                else "add"
            ),
        )

    def equip_armor(self, name: str, item: str, equipped: bool = True) -> dict[str, Any]:
        rules = self.combat
        if rules is None or not rules.defense_base:
            raise InvalidInput("this rule set does not track equipped defense")
        if not isinstance(equipped, bool):
            raise InvalidInput("equipped must be true or false")
        with session_scope(self.engine) as db:
            found = _character_named(load_state(db, self.campaign_id), name)
            held = next((entry for entry in found.items if same_item_name(entry.name, item)), None)
            if held is None:
                raise InvalidInput(f"{found.name} carries no {item!r}")
            listed = self._listed(held.name, include_character=True)
            if listed is None or listed.armor_mode not in {"max", "set", "shield"}:
                raise InvalidInput(f"{held.name} is not equippable armor or a shield")
            if equipped and not held.ready:
                raise InvalidInput(f"{held.name} is stowed; ready it before equipping it")
            is_shield = listed.armor_mode == "shield"
            current = found.equipped_shield if is_shield else found.equipped_armor
            if not equipped and (not current or not same_item_name(current, held.name)):
                raise InvalidInput(f"{found.name} does not have {held.name} equipped")
            selected = (held.name, listed.armor) if equipped else ("", 0)
            armor = combat.set_defense_equipment(
                db,
                self.campaign_id,
                found,
                self.character,
                rules,
                armor=None if is_shield else selected,
                shield=selected if is_shield else None,
            )
            db.commit()
            return {
                "character": found.name,
                "item": held.name,
                "equipped": equipped,
                "armor": armor,
            }

    def _unequip_item(self, db: Session, name: str, item: str) -> None:
        rules = self.combat
        if rules is None or not rules.defense_base:
            return
        found = _character_named(load_state(db, self.campaign_id), name)
        armor = (
            ("", 0) if found.equipped_armor and same_item_name(found.equipped_armor, item) else None
        )
        shield = (
            ("", 0)
            if found.equipped_shield and same_item_name(found.equipped_shield, item)
            else None
        )
        if armor is not None or shield is not None:
            combat.set_defense_equipment(
                db, self.campaign_id, found, self.character, rules, armor=armor, shield=shield
            )

    def give_item(
        self, name: str, item: str, size: str = "normal", damage: str = ""
    ) -> dict[str, Any]:
        listed = self._listed(item)
        if listed is not None and size == "normal":
            size = listed.size
        with session_scope(self.engine) as db:
            return inventory_service.give_item(
                db,
                self.campaign_id,
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                item,
                size,
                listed,
                damage,
            )

    def drop_item(self, name: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = inventory_service.drop_item(
                db, self.campaign_id, _required(self.inventory, "inventory"), name, item
            )
            self._unequip_item(db, name, item)
            db.commit()
            return result

    def ready_item(self, name: str, item: str, ready: bool = True) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = inventory_service.set_ready(
                db,
                self.campaign_id,
                _required(self.inventory, "inventory"),
                name,
                item,
                ready,
            )
            if not ready:
                self._unequip_item(db, name, item)
            db.commit()
            return result

    def use_item(self, name: str, item: str, target: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            found = _character_named(load_state(db, self.campaign_id), name)
            held = next((entry for entry in found.items if same_item_name(entry.name, item)), None)
            if held is not None and held.max_uses > 0 and held.uses == 1:
                self._unequip_item(db, name, item)
            result = aid.use_item(
                db,
                self.campaign_id,
                _required(self.inventory, "inventory"),
                self.combat,
                self.conditions,
                self.rng,
                name,
                item,
                target,
                self.magic.charged.kind if self.magic is not None else "",
                plus=self.plus,
                spec=self.character,
            )
            db.commit()
            return result

    def stabilize(self, name: str, explanation: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return aid.stabilize(
                db,
                self.campaign_id,
                name,
                explanation,
                self._rules(),
                self.conditions,
                self.rng,
                plus=self.plus,
                spec=self.character,
            )

    def give_spell_item(
        self, name: str, spell: str, description: str, *, role: Literal["reusable", "single_use"]
    ) -> dict[str, Any]:
        magic = _required(self.magic, "magic")
        if role == "reusable":
            item_kind = magic.reusable
        elif role == "single_use":
            item_kind = magic.single_use
        else:
            raise InvalidInput(f"unknown spell item role {role!r}")
        with session_scope(self.engine) as db:
            return magic_service.give_spell_item(
                db,
                self.campaign_id,
                item_kind,
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                spell,
                description,
            )

    def give_charged_item(
        self, name: str, item: str, uses: int, recharge: str, size: str = "normal"
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.give_charged_item(
                db,
                self.campaign_id,
                _required(self.magic, "magic"),
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                item,
                uses,
                recharge,
                size,
            )

    def cast_spell(self, name: str, spell: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.cast_spell(
                db,
                self.campaign_id,
                _required(self.magic, "magic"),
                _required(self.inventory, "inventory"),
                name,
                spell,
            )

    def roll_move(
        self, move: str, character: str, modifier: int = 0, stat: str | None = None
    ) -> dict[str, Any]:
        rules = self.moves.get(move)
        if rules is None:
            raise InvalidInput(
                f"there is no move named {move!r}; choose one of {sorted(self.moves)}"
            )
        with session_scope(self.engine) as db:
            return moves_service.make_move(
                db,
                self.campaign_id,
                character,
                rules,
                self.rng,
                plus=self.plus,
                combat=self.combat,
                conditions=self.conditions,
                counters=self.counters,
                modifier=modifier,
                stat=stat,
                spec=self.character,
            )

    def _weapon_feature(self, name: str) -> Feature | None:
        """The feature a player character's attack with `name` uses: the rule set's weapon feature
        of that name that deals damage, looked up among the character's features first and then
        the world's own catalogue."""
        wanted = name.strip()
        if not wanted:
            return None
        held = next(
            (
                feature
                for feature in self.character.features.values()
                if same_item_name(feature.name, wanted) and feature.damage
            ),
            None,
        )
        if held is not None:
            return held
        return next(
            (
                feature
                for feature in self.features.values()
                if same_item_name(feature.name, wanted) and feature.damage
            ),
            None,
        )

    def roll_check(
        self,
        check: str,
        character: str,
        skill: str = "",
        stat: str = "",
        difficulty: int | None = None,
        modifier: int = 0,
        cause: str = "",
    ) -> dict[str, Any]:
        rules = self.checks.get(check)
        if rules is None:
            options = ", ".join(sorted(self.checks)) or "none"
            raise InvalidInput(f"there is no check named {check!r}; the checks are: {options}")
        with session_scope(self.engine) as db:
            return checks_service.roll_check(
                db,
                self.campaign_id,
                character,
                rules,
                self.character,
                self.rng,
                plus=self.plus,
                skill=skill,
                stat=stat,
                difficulty=difficulty,
                modifier=modifier,
                cause=cause,
                conditions=self.conditions,
            )

    def raise_skill(self, name: str, skill: str, level: int, cause: str = "") -> dict[str, Any]:
        found = self.character.skills.get(skill)
        if found is None:
            options = ", ".join(sorted(self.character.skills)) or "none"
            raise InvalidInput(
                f"there is no skill named {skill!r}; the rule set's skills are: {options}"
            )
        progression = self.character.skill_progression
        if progression is None:
            raise InvalidInput("the rule set has no skill progression")
        new_level = whole_number(level, "level", progression.first_level, progression.maximum)
        with session_scope(self.engine) as db:
            character = _character_named(load_state(db, self.campaign_id), name)
            before = self.character.skill_level(character.skills, skill)
            if self.character.skill_trained(character.skills, skill) and new_level <= before:
                raise InvalidInput(
                    f"{character.name}'s {found.name} is already at level {before}; "
                    "raise_skill only raises a skill"
                )
            record(
                db,
                self.campaign_id,
                SkillChanged(
                    name=character.name,
                    skill=skill,
                    skill_name=found.name,
                    before=before,
                    after=new_level,
                    cause=bounded_text(cause, "cause", MAX_TEXT),
                ),
            )
            db.commit()
            return {
                "character": character.name,
                "skill": skill,
                "skill_name": found.name,
                "before": before,
                "level": new_level,
            }

    def restore_stat(self, name: str, stat: str, amount: int, cause: str = "") -> dict[str, Any]:
        value = whole_number(amount, "amount", 1, MAX_STAT)
        with session_scope(self.engine) as db:
            character = _character_named(load_state(db, self.campaign_id), name)
            if stat not in character.stats:
                options = ", ".join(sorted(character.stats)) or "none"
                raise InvalidInput(
                    f"{character.name} has no stat {stat!r}; the stats are: {options}"
                )
            restored = recover(character, stat, value)
            record(
                db,
                self.campaign_id,
                Recovered(
                    name=character.name,
                    stat=stat,
                    amount=restored,
                    cause=bounded_text(cause, "cause", MAX_TEXT),
                ),
            )
            db.commit()
            return {
                "character": character.name,
                "stat": stat,
                "restored": restored,
                "current": current(character, stat),
            }

    def set_stat_max(self, name: str, stat: str, value: int, cause: str = "") -> dict[str, Any]:
        new_value = whole_number(value, "value", 0, MAX_STAT)
        with session_scope(self.engine) as db:
            character = _character_named(load_state(db, self.campaign_id), name)
            if stat not in character.stats:
                options = ", ".join(sorted(character.stats)) or "none"
                raise InvalidInput(
                    f"{character.name} has no stat {stat!r}; the stats are: {options}"
                )
            before = character.stats[stat]
            record(
                db,
                self.campaign_id,
                MaxChanged(
                    name=character.name,
                    stat=stat,
                    before=before,
                    after=new_value,
                    cause=bounded_text(cause, "cause", MAX_TEXT),
                ),
            )
            if (
                self.combat is not None
                and self.combat.defense_base
                and stat == self.combat.defense_modifier
                and character.defense_tracked
            ):
                combat.recalculate_defense(
                    db, self.campaign_id, character.name, self.character, self.combat
                )
            db.commit()
            return {
                "character": character.name,
                "stat": stat,
                "before": before,
                "maximum": new_value,
            }

    def adjust_counter(self, character: str, counter: str, change: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return counters_service.adjust_counter(
                db,
                self.campaign_id,
                character,
                counter,
                change,
                counters=self.counters,
                conditions=self.conditions,
                combat=self.combat,
                rng=self.rng,
                plus=self.plus,
                spec=self.character,
            )

    def take_advancement(
        self,
        character: str,
        option: str,
        stat: str | None = None,
        feature: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = advancements_service.take_advancement(
                db,
                self.campaign_id,
                character,
                option,
                advancements=next(iter(self.advancements.values()), None),
                counters=self.counters,
                conditions=self.conditions,
                combat=self.combat,
                rng=self.rng,
                plus=self.plus,
                stat=stat,
                feature=feature,
                features=self.character.features,
                spec=self.character,
            )
            if self.combat is not None and self.combat.defense_base:
                found = _character_named(load_state(db, self.campaign_id), character)
                if found.defense_tracked:
                    combat.recalculate_defense(
                        db, self.campaign_id, found.name, self.character, self.combat
                    )
                    db.commit()
            return result

    def end_period(self, period: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return periods_service.end_period(
                db,
                self.campaign_id,
                self.periods.get(period),
                counters=self.counters,
                conditions=self.conditions,
                combat=self.combat,
                tallies=self.tallies,
                rng=self.rng,
                plus=self.plus,
                spec=self.character,
            )

    def adjust_tally(
        self,
        tally: str,
        entry: str,
        change: int,
        reason: str = "",
        award: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return tallies_service.adjust_tally(
                db,
                self.campaign_id,
                self.tallies.get(tally),
                entry,
                change,
                reason=reason,
                award=award,
                force=force,
            )

    def set_relation(
        self, character: str, relation: str, other: str, added: bool
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return relations_service.set_relation(
                db,
                self.campaign_id,
                character,
                self.relations.get(relation),
                other,
                added=added,
            )

    def use_charge(self, name: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.use_charge(
                db, self.campaign_id, _required(self.magic, "magic"), name, item
            )

    def recharge_charged_item(self, name: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.recharge_charged_item(
                db, self.campaign_id, _required(self.magic, "magic"), name, item
            )

    def dungeon_cycle(self, triggers: list[str], reason: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return dungeon_cycle_service(
                db,
                self.campaign_id,
                triggers,
                reason,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                rules=dungeon_rules(self.rule_data),
            )

    def wilderness_action(
        self, action: str, reason: str, purchases: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Take a Wilderness Action for the current watch, then spend that watch. The action rolls
        the day's weather and the events; the last part of the day counts as night. A resupply
        names its purchases, which are bought at the marketplace."""
        wilderness = _required(self.wilderness, "wilderness")
        clock_rules = _required(self.clock_rules, "time")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            day = state.clock.day
            night = state.clock.part == len(clock_rules.parts) - 1
            season = wilderness_service.season_for_day(
                wilderness, state.start_season or wilderness.start_season, day
            )
            # Living PCs and hirelings. Player characters come first, so a grant goes to one of them.
            party = sorted(
                (c for c in state.characters.values() if c.kind == PC_KIND and not c.dead),
                key=lambda c: c.hireling,
            )
            result = wilderness_service.wilderness_action(
                db,
                self.campaign_id,
                action,
                reason,
                wilderness,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                day=day,
                season=season,
                at=clock_service.absolute(
                    state.clock, len(clock_rules.parts), clock_rules.hours_per_part
                ),
                party=[c.name for c in party],
                night=night,
                give=self._give_catalogue_item,
                hooks=self.hooks,
                buy=self._buy_catalogue_item,
                purchases=purchases,
            )
        with session_scope(self.engine) as db:
            result["clock"] = clock_service.advance_time(
                db, self.campaign_id, clock_rules, self.hooks, self.inventory, days=0, parts=1
            )
        return result

    def roll_reaction(self, reason: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return roll_reaction_service(
                db,
                self.campaign_id,
                reason,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                rules=dungeon_rules(self.rule_data),
            )

    def roll_save(
        self, name: str, stat: str, reason: str, natural: int | None = None
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.roll_save(
                db,
                self.campaign_id,
                _required(self.combat, "combat"),
                self.character,
                self.rng,
                name,
                stat,
                reason,
                plus=self.plus,
                natural=natural,
            )

    def place_names(self) -> list[str]:
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            return sorted(place.name for place in state.places.values())

    def create_location(
        self,
        name: str,
        description: str,
        discovered: bool = False,
        x: float | None = None,
        y: float | None = None,
        force: bool = False,
        parent: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_location(
                db, self.campaign_id, name, description, discovered, x, y, force, parent
            )

    def discover_location(self, name: str, summary: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return discover_location(db, self.campaign_id, name, summary)

    def create_npc(
        self,
        name: str,
        location: str,
        description: str,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        character: Mapping[str, Any] | None = None,
        force: bool = False,
        hp_tier: str | None = None,
        pronouns: str | None = None,
        important: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = create_npc(
                db,
                self.campaign_id,
                self.tables,
                self.rng,
                self.character,
                name,
                location,
                description,
                personality,
                accent,
                quirks,
                character,
                monsters=self.monsters,
                force=force,
                plus=self.plus,
                npc_hp=self.npc_hp,
                npc_hp_stat=self.npc_hp_stat,
                hp_tier=hp_tier,
                pronouns=pronouns,
                important=important,
            )
            if result["statblock"] is None:
                self._initialize_generated_defense(db, name)
            db.commit()
            return result

    def hire_hireling(
        self,
        name: str,
        employer: str,
        wage: int,
        description: str,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        character: Mapping[str, Any] | None = None,
        pronouns: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = hire_hireling(
                db,
                self.campaign_id,
                self.tables,
                self.rng,
                self.character,
                name,
                employer,
                wage,
                description,
                personality,
                accent,
                quirks,
                character,
                pronouns=pronouns,
                plus=self.plus,
            )
            self._initialize_generated_defense(db, name)
            db.commit()
            return result

    def _initialize_generated_defense(self, db: Session, name: str) -> None:
        _initialize_generated_defense(
            db,
            self.campaign_id,
            name,
            self.character,
            self.combat,
            self.inventory,
            self.item_sizes,
            self.magic,
            self.marketplace.currency if self.marketplace else "",
        )

    def promote_hireling(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return promote_hireling(db, self.campaign_id, name)

    def dismiss_hireling(self, name: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return dismiss_hireling(db, self.campaign_id, name, cause)

    def move_actor(
        self,
        name: str,
        location: str | None = None,
        travel: str | None = None,
        mode: str = IN_SETTLEMENT,
        route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in MOVE_MODES:
            names = ", ".join(MOVE_MODES)
            raise InvalidInput(f"there is no move mode named {mode!r}; use one of {names}")
        if mode == OVERLAND and location is not None:
            with session_scope(self.engine) as db:
                actor = _character_named(load_state(db, self.campaign_id), name)
            if actor.kind == PC_KIND:
                return self._overland_move(actor.name, location, route)
        with session_scope(self.engine) as db:
            return move_actor(
                db,
                self.campaign_id,
                name,
                location,
                travel=self.travel,
                speed=travel,
                mode=mode,
                clock_rules=self.clock_rules,
                hooks=self.hooks,
                inventory=self.inventory,
            )

    def _overland_move(
        self, name: str, location: str, route: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """An overland move takes watches. Each watch rolls for the party getting lost, takes the
        Travel Wilderness Action (weather, events, night hooks), and advances the clock one part.
        A lost watch makes the next one a recovery that makes no progress. The move places the
        character once the last progress watch is spent."""
        wilderness = _required(self.wilderness, "wilderness")
        clock_rules = _required(self.clock_rules, "time")
        inventory = _required(self.inventory, "inventory")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            actor = _character_named(state, name)
            where = _place_named(state, location)
            origin = state.places.get(name_key(actor.location)) if actor.location else None
            authored = _authored_route(state, origin, where)
            if authored is not None:
                # The world's route supplies the path, distance, terrain, and vast; only the
                # weather choice comes from the GM.
                effective_route: dict[str, Any] = {}
                if route is not None and "weather_choice" in route:
                    effective_route["weather_choice"] = route["weather_choice"]
                effective_route.update(authored)
                route = effective_route
            if route is None:
                raise InvalidInput(
                    "an overland move needs a route with path, distance, and terrain"
                )
            party = _travelling_party(state, actor)
            start = state.clock
            season = wilderness_service.season_for_day(
                wilderness, state.start_season or wilderness.start_season, start.day
            )
            hc = HookContext(db, self.campaign_id, self.hooks, self.inventory)
            held = any(call_hook(self.hooks, "held_back", hc, c.name, default="") for c in party)
            weather = wilderness_service.roll_weather(
                db,
                self.campaign_id,
                wilderness,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                day=start.day,
                season=season,
            )
            plan = wilderness_service.plan_overland(wilderness, route, weather["weather"], held)
            if plan.fatigue:
                for member in party:
                    inventory_service.add_penalty(
                        db, self.campaign_id, inventory, member.name, cause="travel"
                    )
        journey: list[dict[str, Any]] = []
        progress = 0
        recovering = False
        while progress < plan.watches:
            with session_scope(self.engine) as db:
                state = load_state(db, self.campaign_id)
                clock = state.clock
                season = wilderness_service.season_for_day(
                    wilderness, state.start_season or wilderness.start_season, clock.day
                )
                names = [m.name for m in _travelling_party(state, _character_named(state, name))]
                lost = wilderness_service.roll_lost(
                    db,
                    self.campaign_id,
                    route["path"],
                    wilderness,
                    self.rng,
                    reason=f"overland travel to {where.name}",
                )
                watch = wilderness_service.wilderness_action(
                    db,
                    self.campaign_id,
                    wilderness_service.TRAVEL_ACTION,
                    f"overland travel to {where.name}",
                    wilderness,
                    self.dice_tables,
                    self.rng,
                    plus=self.plus,
                    day=clock.day,
                    season=season,
                    at=clock_service.absolute(
                        clock, len(clock_rules.parts), clock_rules.hours_per_part
                    ),
                    party=names,
                    night=clock.part == len(clock_rules.parts) - 1,
                    give=self._give_catalogue_item,
                    travel_watch=True,
                )
            with session_scope(self.engine) as db:
                clock_now = clock_service.advance_time(
                    db, self.campaign_id, clock_rules, self.hooks, self.inventory, days=0, parts=1
                )
            made_progress = not recovering
            if made_progress:
                progress += 1
            journey.append(
                {
                    "lost": lost["lost"],
                    "roll": lost["roll"],
                    "progress": made_progress,
                    "weather": watch["weather"],
                    "events": watch["events"],
                    "effects": watch["effects"],
                    "clock": clock_now,
                }
            )
            recovering = lost["lost"]
        with session_scope(self.engine) as db:
            result = move_actor(
                db,
                self.campaign_id,
                name,
                location,
                travel=self.travel,
                mode=OVERLAND,
                clock_rules=self.clock_rules,
                hooks=self.hooks,
                inventory=self.inventory,
            )
        result.update(
            {
                "watches": plan.watches,
                "terrain": plan.terrain,
                "fatigue": plan.fatigue,
                "lost_watches": sum(1 for w in journey if w["lost"]),
                "journey": journey,
            }
        )
        return result

    def update_location(
        self,
        name: str,
        description: str,
        x: float | None = None,
        y: float | None = None,
        off_map: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_location(db, self.campaign_id, name, description, x, y, off_map, force)

    def delete_location(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_location(db, self.campaign_id, name)

    def update_npc(
        self,
        name: str,
        description: str | None = None,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        character: Mapping[str, Any] | None = None,
        armor: int | None = None,
        attacks: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = update_npc(
                db,
                self.campaign_id,
                self.character,
                self.rng,
                name,
                description,
                personality,
                accent,
                quirks,
                character,
                armor,
                attacks,
                armor_max=self.combat.armor_max if self.combat else 0,
                defense_base=self.combat.defense_base if self.combat else 0,
                plus=self.plus,
            )
            if result["statted"] and armor is None:
                self._initialize_generated_defense(db, name)
            db.commit()
            return result

    def update_npc_knowledge(
        self,
        name: str,
        description: str | None = None,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        pronouns: str | None = None,
        relationship: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_npc_knowledge(
                db,
                self.campaign_id,
                name,
                description=description,
                personality=personality,
                accent=accent,
                quirks=quirks,
                pronouns=pronouns,
                relationship=relationship,
                location=location,
            )

    def delete_npc(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_npc(db, self.campaign_id, name)

    def create_encounter_map(
        self,
        name: str,
        location: str,
        columns: int | None = None,
        rows: int | None = None,
        description: str = "",
        template: str = "",
    ) -> dict[str, Any]:
        """Make a map at a location. A template may supply the size, so `columns` and `rows` are
        optional, and every tool that makes a map requires its description."""
        with session_scope(self.engine) as db:
            return encounters.create_map(
                db,
                self.campaign_id,
                name,
                location,
                columns,
                rows,
                description,
                template,
                self.encounter_templates,
            )

    def set_encounter_map_cells(self, map: str, cells: list[Mapping[str, Any]]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.set_map_cells(
                db, self.campaign_id, map, cells, self.terrain, self.encounter_objects
            )

    def update_encounter_map(
        self,
        name: str,
        columns: int,
        rows: int,
        description: str,
        new_name: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.update_map(
                db, self.campaign_id, name, columns, rows, description, new_name, location
            )

    def delete_encounter_map(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.delete_map(db, self.campaign_id, name)

    def start_encounter(self, map: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.start_encounter(
                db,
                self.campaign_id,
                map,
                rolls_initiative=self.combat is not None,
                combat=self.combat,
                character=self.character,
                rng=self.rng,
            )

    def end_encounter(self) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.end_encounter(db, self.campaign_id)

    def add_participant(self, name: str, side: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = encounters.add_participant(db, self.campaign_id, name, side)
        if self.combat is not None and self.combat.initiative is not None:
            with session_scope(self.engine) as db:
                state = load_state(db, self.campaign_id)
                encounter = state.encounter
                if (
                    encounter is not None
                    and not encounter.settled
                    and len({encounter.sides.get(name_key(n), "") for n in encounter.order}) > 1
                ):
                    result["initiative"] = encounters.roll_side_initiative(
                        db, self.campaign_id, self.combat, self.character, self.rng
                    )
        return result

    def remove_participant(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.remove_participant(
                db, self.campaign_id, name, self._initiative_order()
            )

    def create_monster(
        self,
        name: str,
        location: str,
        description: str | None = None,
        template: str | None = None,
        stats: Mapping[str, int | None] | None = None,
        armor: int | None = None,
        attacks: Sequence[Mapping[str, Any]] | None = None,
        force: bool = False,
        detachment: bool | None = None,
        rules: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_monster(
                db,
                self.campaign_id,
                self.monsters,
                self.character,
                name,
                location,
                description,
                template,
                stats or {},
                armor,
                attacks,
                armor_max=self.combat.armor_max if self.combat else 0,
                force=force,
                detachment=detachment,
                rules=rules,
            )

    def search_monster(self, query: str) -> dict[str, list[dict[str, Any]]]:
        return search_monster(self.monsters, query)

    def search_marketplace(self, query: str) -> dict[str, Any]:
        return search_marketplace(_required(self.marketplace, "a marketplace"), query)

    def attack(
        self,
        attacker: str,
        attack: str,
        target: str,
        modifier: str = "",
        targets: Sequence[str] | None = None,
        hit_modifier: int = 0,
        count: int = 0,
    ) -> dict[str, Any]:
        attacks = count or self.attacks_per_action(attacker)
        if attacks > 1 and targets is None:
            entries = [
                {
                    "attacker": attacker,
                    "attack": attack,
                    "modifier": modifier,
                    "hit_modifier": hit_modifier,
                }
                for _ in range(attacks)
            ]
            return self.resolve_attacks(target, entries)
        with session_scope(self.engine) as db:
            return combat.attack(
                db,
                self.campaign_id,
                attacker,
                attack,
                target,
                self._rules(),
                self.rng,
                modifier,
                plus=self.plus,
                targets=targets,
                hit_modifier=hit_modifier,
                conditions=self.conditions,
                spec=self.character,
            )

    def attacks_per_action(self, name: str) -> int:
        """The number of attacks a statblock makes in one action, or 1 for a custom NPC."""
        with session_scope(self.engine) as db:
            character = load_state(db, self.campaign_id).characters.get(name_key(name))
            if character is None:
                return 1
            template = next(
                (
                    monster
                    for monster in self.monsters.values()
                    if monster.name.casefold() == character.creature.casefold()
                ),
                None,
            )
            return template.attacks_per_action if template is not None else 1

    def resolve_attacks(self, target: str, attacks: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.resolve_attacks(
                db,
                self.campaign_id,
                target,
                attacks,
                self._rules(),
                self.rng,
                plus=self.plus,
                conditions=self.conditions,
                spec=self.character,
            )

    def player_attack(
        self,
        target: str,
        weapon: str,
        modifier: str = "",
        attack_roll: int | None = None,
        damage_roll: int | None = None,
        hit_modifier: int = 0,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.player_attack(
                db,
                self.campaign_id,
                target,
                weapon,
                self._rules(),
                self.rng,
                modifier,
                plus=self.plus,
                spec=self.character,
                weapon_feature=self._weapon_feature(weapon),
                attack_roll=attack_roll,
                damage_roll=damage_roll,
                hit_modifier=hit_modifier,
                conditions=self.conditions,
            )

    def attack_dice(self, weapon: str, modifier: str = "") -> str:
        with session_scope(self.engine) as db:
            return combat.attack_dice(db, self.campaign_id, weapon, self._rules(), modifier)

    def resolve_group_break(self, name: str, outcome: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.resolve_group_break(db, self.campaign_id, name, outcome, self._rules())

    def modifier_dice(self, modifier: str) -> str | None:
        return attack_modifiers.modifier_dice(self._rules().modifiers, modifier)

    def player_save(self, roll: int = 0) -> dict[str, Any]:
        """Record the player's own d20 for a save the combat rules have marked due."""
        with session_scope(self.engine) as db:
            return combat.player_save(
                db,
                self.campaign_id,
                roll,
                self._rules(),
                conditions=self.conditions,
                rng=self.rng,
                plus=self.plus,
                spec=self.character,
            )

    def player_initiative(self, roll: int = 0) -> dict[str, Any]:
        """Record the player's own d20 for initiative in the first round."""
        with session_scope(self.engine) as db:
            return combat.player_initiative(db, self.campaign_id, roll, self._rules())

    def roll_side_initiative(self) -> dict[str, Any]:
        """Roll once for each side of the encounter, once its participants have joined."""
        with session_scope(self.engine) as db:
            return encounters.roll_side_initiative(
                db, self.campaign_id, self.combat, self.character, self.rng
            )

    def set_armor(self, name: str, armor: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.set_armor(
                db,
                self.campaign_id,
                name,
                armor,
                self._rules().armor_max,
                spec=self.character,
                rules=self.combat,
            )

    def recalculate_defense(self, name: str) -> dict[str, Any]:
        rules = self.combat
        if rules is None or not rules.defense_base:
            raise InvalidInput("this rule set does not track equipped defense")
        with session_scope(self.engine) as db:
            found = _character_named(load_state(db, self.campaign_id), name)
            if not found.defense_tracked:
                raise InvalidInput(f"{found.name}'s equipped defense has not been recorded")
            value = combat.recalculate_defense(
                db, self.campaign_id, found.name, self.character, rules
            )
            db.commit()
            return {"character": found.name, "armor": value}

    def recover_character(self, name: str, explanation: str, consequence: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.recover_character(
                db, self.campaign_id, name, explanation, consequence, self._rules()
            )

    def heal_character(
        self, name: str, amount: int, stat: str | None = None, cause: str = ""
    ) -> dict[str, Any]:
        if stat is None:
            if self.combat is None or self.combat.healing_stat is None:
                raise InvalidInput("this rule set has no default healing stat")
            stat = self.combat.healing_stat
        with session_scope(self.engine) as db:
            return combat.heal_character(db, self.campaign_id, name, stat, amount, cause)

    def harm_character(self, name: str, stat: str, amount: int, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.harm_character(
                db, self.campaign_id, name, stat, amount, cause, self._rules()
            )

    def set_debug_stat(self, name: str, stat: str, remaining: int) -> dict[str, Any]:
        """Set a player character's current score for one stat, for testing damage rules."""
        with session_scope(self.engine) as db:
            return combat.set_debug_stat(db, self.campaign_id, name, stat, remaining)

    def add_condition(self, name: str, condition: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return add_condition(
                db, self.campaign_id, name, condition, cause, self.conditions, self.combat
            )

    def end_condition(self, name: str, condition: str, how: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return end_condition(
                db,
                self.campaign_id,
                name,
                condition,
                how,
                self.combat,
                self.conditions,
                self.rng,
                plus=self.plus,
                spec=self.character,
            )

    def retreat(self, name: str, safe: bool) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return morale.retreat(
                db, self.campaign_id, name, safe, self._rules(), self.rng, plus=self.plus
            )

    def player_retreat(self, safe: bool, roll: int = 0) -> dict[str, Any]:
        """Record the player's own d20 for a retreat."""
        with session_scope(self.engine) as db:
            return morale.player_retreat(db, self.campaign_id, safe, roll, self._rules())

    def surrender(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.surrender(db, self.campaign_id, name, self._rules())

    def _rules(self) -> CombatRules:
        if self.combat is None:
            raise InvalidInput("this rule set has no combat")
        return self.combat

    def _initiative_order(self) -> encounters.InitiativeOrder | None:
        return self.combat.initiative_order if self.combat is not None else None

    def place_participant(self, name: str, column: int, row: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.place_participant(
                db,
                self.campaign_id,
                name,
                column,
                row,
                self.grid,
                self.terrain,
                self.encounter_objects,
            )

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return add_note(db, self.campaign_id, text, subject)

    def update_note(self, note_id: int, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_note(db, self.campaign_id, note_id, text)

    def delete_note(self, note_id: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_note(db, self.campaign_id, note_id)

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_notes(load_state(db, self.campaign_id), query)

    def append_journal(self, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return append_journal(db, self.campaign_id, text)

    def replace_in_journal(self, old_text: str, new_text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return replace_in_journal(db, self.campaign_id, old_text, new_text)

    def search_journal(self, query: str, context: int = 1) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_journal(load_state(db, self.campaign_id), query, context)

    def view_journal(self, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return view_journal(load_state(db, self.campaign_id), start_line, end_line)

    def all_at_location(self, location: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return all_at_location(load_state(db, self.campaign_id), location)

    def search_location(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_location(load_state(db, self.campaign_id), query)

    def search_npc(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_npc(load_state(db, self.campaign_id), query)

    def get_statblock(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return get_statblock(
                load_state(db, self.campaign_id),
                name,
                self.character,
                self.combat,
                self.conditions,
                self.inventory,
                self.hooks,
                self.counters,
            )

    def character_options(self, field: str | None = None) -> dict[str, Any]:
        return choice_options(self.character, field)

    def start_project(
        self,
        name: str,
        kind: str,
        goal: str,
        target: int,
        force: bool = False,
    ) -> dict[str, Any]:
        """Begin a project, research effort, magic working, or faction turn."""
        name = name.strip()
        kind = kind.strip()
        goal = goal.strip()
        if not name or not kind or not goal:
            raise InvalidInput("a project needs a name, a kind, and a goal")
        if isinstance(target, bool) or not isinstance(target, int) or target < 1:
            raise InvalidInput("a project's target must be a whole number, 1 or more")
        with session_scope(self.engine) as db:
            existing = load_state(db, self.campaign_id).projects.get(name_key(name))
            if existing is not None and not force:
                raise InvalidInput(f"there is already a project named {name!r}")
            record(
                db,
                self.campaign_id,
                ProjectStarted(name=name, project_kind=kind, goal=goal, target=target),
            )
            db.commit()
            return self._project_view(db, name)

    def advance_project(self, name: str, amount: int, note: str = "") -> dict[str, Any]:
        """Add progress to a project, completing it when it reaches its target."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise InvalidInput("progress must be a whole number, 1 or more")
        with session_scope(self.engine) as db:
            project = load_state(db, self.campaign_id).projects.get(name_key(name))
            if project is None:
                raise InvalidInput(f"there is no project named {name!r}")
            if project.completed:
                raise InvalidInput(f"{project.name} is already complete")
            if project.abandoned:
                raise InvalidInput(f"{project.name} was abandoned")
            record(
                db,
                self.campaign_id,
                ProjectAdvanced(name=project.name, amount=amount, note=note.strip()),
            )
            db.commit()
            return self._project_view(db, project.name)

    def abandon_project(self, name: str, reason: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            project = load_state(db, self.campaign_id).projects.get(name_key(name))
            if project is None:
                raise InvalidInput(f"there is no project named {name!r}")
            record(db, self.campaign_id, ProjectAbandoned(name=project.name, reason=reason.strip()))
            db.commit()
            return self._project_view(db, project.name)

    def list_projects(self) -> list[dict[str, Any]]:
        with session_scope(self.engine) as db:
            return [
                self._project_view(db, project.name)
                for project in load_state(db, self.campaign_id).projects.values()
            ]

    def _project_view(self, db: Any, name: str) -> dict[str, Any]:
        project = load_state(db, self.campaign_id).projects[name_key(name)]
        return {
            "name": project.name,
            "kind": project.kind,
            "goal": project.goal,
            "progress": project.progress,
            "target": project.target,
            "completed": project.completed,
            "abandoned": project.abandoned,
            "notes": list(project.notes),
        }

    def campaign_options(self) -> dict[str, Any]:
        """The options chosen once for the campaign, such as its experience track."""
        with session_scope(self.engine) as db:
            return dict(load_state(db, self.campaign_id).options)

    def lookup_move(self, move: str) -> dict[str, Any]:
        """A move's full definition, so the GM can see when it triggers and what its bands do."""
        found = self.moves.get(move.strip())
        if found is None:
            found = next(
                (
                    candidate
                    for candidate in self.moves.values()
                    if candidate.label.casefold() == move.strip().casefold()
                ),
                None,
            )
        if found is None:
            raise InvalidInput(f"there is no move named {move!r}")
        return {
            "id": found.id,
            "label": found.label,
            "description": found.description,
            "dice": found.dice,
            "stat": found.stat,
            "stat_choice": list(found.stat_choice),
            "skill": found.skill,
            "bands": [
                {
                    "label": band.label,
                    "min": band.min,
                    "text": band.text,
                }
                for band in found.bands
            ],
        }

    def lookup_condition(self, condition: str) -> dict[str, Any]:
        """A condition's full definition, so the GM can read what it does and how it ends."""
        wanted = condition.strip()
        found = self.conditions.get(wanted)
        if found is None:
            found = next(
                (
                    candidate
                    for candidate in self.conditions.values()
                    if candidate.label.casefold() == wanted.casefold()
                ),
                None,
            )
        if found is None:
            raise InvalidInput(f"there is no condition named {condition!r}")
        return {
            "id": found.id,
            "label": found.label,
            "description": found.description,
            "ends": list(found.ends),
            "on_end": [dict(effect) for effect in found.on_end],
            "modifies": dict(found.modifies),
            "dying": found.dying,
            "fatal": found.fatal,
        }

    def lookup_feature(self, feature: str) -> dict[str, Any]:
        """A feature's full definition, by id or name, so the GM can read a spell, art, Focus,
        or class ability before resolving it."""
        wanted = feature.strip()
        found = self.character.features.get(wanted)
        if found is None:
            found = next(
                (
                    candidate
                    for candidate in self.character.features.values()
                    if candidate.name.casefold() == wanted.casefold()
                ),
                None,
            )
        if found is None:
            raise InvalidInput(f"there is no feature named {feature!r}")
        return {
            "id": found.id,
            "name": found.name,
            "feature_type": found.feature_type,
            "tag": found.tag,
            "rank": found.rank,
            "description": found.description,
            "effects": [dict(effect) for effect in found.effects],
            "skill": found.skill,
            "attributes": list(found.attributes),
            "shock": found.shock,
            "shock_ac": found.shock_ac,
            "flags": list(found.flags),
            "rules": list(found.rules),
        }

    def lookup_index(self) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return lookup_index(load_state(db, self.campaign_id))

    def speaker_profile(self, name: str) -> str:
        with session_scope(self.engine) as db:
            return speaker_profile(load_state(db, self.campaign_id), name, self.character)

    def speaker_at_player(self, name: str) -> bool:
        with session_scope(self.engine) as db:
            return speaker_at_player(load_state(db, self.campaign_id), name)


def seed_campaign(
    db: Session,
    campaign_id: int,
    world: WorldTemplate,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    *,
    monsters: Mapping[str, MonsterTemplate],
    plus: str,
    npc_hp: Mapping[str, int],
    npc_hp_stat: str | None,
    wilderness: Wilderness | None = None,
    combat_rules: CombatRules | None = None,
    inventory: InventoryRules | None = None,
    item_sizes: Mapping[str, int] | None = None,
    magic: MagicRules | None = None,
    currency: str = "",
) -> None:
    """Record a campaign's places and the world's NPCs as events. The player character is made
    later, by `create_player_character`. An NPC that names a statblock takes its stats, and an
    NPC with an HP tier and no statblock takes that tier's HP. A rule set with a wilderness
    calendar also records the season the campaign starts in."""
    if wilderness is not None:
        requested = world.start.season if world.start is not None else None
        season = wilderness_service.start_season_for(wilderness, requested)
        record(db, campaign_id, SeasonSet(season=season))
    # A place can be declared before the place it is inside, so every name is known first.
    names = {spot.id: spot.name for spot in world.locations}
    for spot in world.locations:
        record(
            db,
            campaign_id,
            LocationAdded(
                name=spot.name,
                description=spot.description,
                discovered=spot.discovered,
                x=spot.x,
                y=spot.y,
                parent=None if spot.parent is None else names[spot.parent],
                icon=spot.icon,
                rules=list(spot.rules),
                routes={key: dict(route) for key, route in spot.routes.items()},
            ),
        )
    for region in world.regions:
        record(
            db,
            campaign_id,
            RegionAdded(
                name=region.name,
                terrain=region.terrain,
                points=[[x, y] for x, y in region.points],
            ),
        )
    for npc in world.npcs:
        base = None if npc.monster is None else monsters[npc.monster]
        if base is not None:
            character: dict[str, Any] | None = {"stats": dict(base.stats)}
        elif npc.hp_tier is not None and npc_hp_stat is not None:
            character = {"stats": {npc_hp_stat: npc_hp[npc.hp_tier]}}
        else:
            character = None
        event = _npc_event(
            tables,
            rng,
            spec,
            name=npc.name,
            location=names[npc.location],
            description=npc.description,
            personality=npc.personality,
            accent=npc.accent,
            quirks=npc.quirks,
            character=character,
            plus=plus,
            story_only=npc.story_only,
            rules=npc.rules,
        )
        if base is not None:
            event = _with_statblock(event, base)
        record(db, campaign_id, event)
        if base is None and not event.story_only:
            _initialize_generated_defense(
                db,
                campaign_id,
                event.name,
                spec,
                combat_rules,
                inventory,
                item_sizes,
                magic,
                currency,
            )
    db.commit()


def set_campaign_options(
    db: Session, campaign_id: int, campaign: CampaignSpec, submitted: Mapping[str, Any]
) -> dict[str, Any]:
    """Record the campaign's shared options. They are chosen once, so the campaign's omen and
    the like cannot change after the characters are made."""
    if not campaign.fields:
        raise InvalidInput("this campaign has no options to choose")
    state = load_state(db, campaign_id)
    if state.options:
        raise InvalidInput("the campaign options are already chosen")
    problem = check_arguments(campaign_schema(campaign), dict(submitted))
    if problem is not None:
        raise InvalidInput(problem)
    record(db, campaign_id, CampaignOptionsChosen(options=dict(submitted)))
    return {"chosen": "campaign"}


def _starting_item(
    feature: Feature, magic: MagicRules | None, item_sizes: Mapping[str, int]
) -> Item:
    """The inventory item a starting feature becomes: a magic item for a spell, else the item."""
    if feature.feature_type == "spell":
        assert magic is not None
        return Item(
            feature.name,
            magic.reusable.size,
            item_sizes[magic.reusable.size],
            kind=magic.reusable.kind,
            text=feature.description,
        )
    flags = list(feature.flags)
    if feature.armor > 0 and "shield" not in flags and "armor" not in flags:
        flags.append("armor")
    return Item(
        feature.name,
        feature.size,
        item_sizes[feature.size],
        damage=feature.damage,
        rules=feature.rules,
        flags=tuple(flags),
        uses=feature.uses,
        max_uses=feature.uses,
    )


def _take_starting_items(
    spec: CharacterSpec,
    starting: dict[str, int],
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
    magic: MagicRules | None,
    currency: str,
) -> list[tuple[Feature, int]]:
    carried: list[tuple[Feature, int]] = []
    if inventory is not None and item_sizes is not None:
        for feature_id in list(starting):
            feature = spec.features[feature_id]
            if feature_id != currency and (
                feature.feature_type == "item"
                or (feature.feature_type == "spell" and magic is not None)
            ):
                carried.append((feature, starting.pop(feature_id)))
    return carried


def _carry_starting_items(
    db: Session,
    campaign_id: int,
    name: str,
    carried: Sequence[tuple[Feature, int]],
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
    magic: MagicRules | None,
    *,
    defense: bool,
) -> tuple[tuple[str, int], tuple[str, int], int]:
    armor = ("", 0)
    shield = ("", 0)
    armor_bonus = 0
    if carried:
        starting_inventory = _required(inventory, "inventory")
        starting_sizes = _required(item_sizes, "item sizes")
        for feature, count in carried:
            for _ in range(count):
                inventory_service.carry_item(
                    db,
                    campaign_id,
                    starting_inventory,
                    name,
                    _starting_item(feature, magic, starting_sizes),
                    cause="starting gear",
                )
            if defense:
                if "shield" in feature.flags and feature.armor > shield[1]:
                    shield = (feature.name, feature.armor)
                elif "shield" not in feature.flags and feature.armor > armor[1]:
                    armor = (feature.name, feature.armor)
            else:
                armor_bonus += feature.armor * count
    return armor, shield, armor_bonus


def _initialize_generated_defense(
    db: Session,
    campaign_id: int,
    name: str,
    spec: CharacterSpec,
    rules: CombatRules | None,
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
    magic: MagicRules | None,
    currency: str,
) -> None:
    if rules is None or not rules.defense_base:
        return
    character = _character_named(load_state(db, campaign_id), name)
    if character.story_only or character.defense_tracked:
        return
    starting = dict(character.features)
    carried = _take_starting_items(spec, starting, inventory, item_sizes, magic, currency)
    if carried:
        record(
            db,
            campaign_id,
            StartingItemsTransferred(name=character.name, features=[f.id for f, _ in carried]),
        )
    armor, shield, _ = _carry_starting_items(
        db, campaign_id, name, carried, inventory, item_sizes, magic, defense=True
    )
    combat.set_defense_equipment(
        db,
        campaign_id,
        character,
        spec,
        rules,
        armor=armor,
        shield=shield,
        adjustment=character.armor,
    )


def create_player_character(
    db: Session,
    campaign_id: int,
    campaign: CampaignSpec,
    spec: CharacterSpec,
    name: str,
    rolled: Mapping[str, int],
    submitted: Mapping[str, Any],
    rng: random.Random,
    inventory: InventoryRules | None = None,
    item_sizes: Mapping[str, int] | None = None,
    magic: MagicRules | None = None,
    currency: str = "",
    armor_max: int = 0,
    defense_base: int = 0,
    defense_modifier: str = "",
    *,
    pronouns: str,
    plus: str,
    combat_rules: CombatRules | None = None,
) -> dict[str, Any]:
    """Make the player character from the player's choices. `rolled` are the stats rolled for
    this character; the player may only keep them or swap within a swap group. Starting item
    features are carried as inventory items, spell features as the rule set's magic items, and the armor they
    give is worn. The currency stays a count. The pronouns are how the GM refers to the character."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character already exists")
    if campaign.fields and not state.options:
        raise InvalidInput("the campaign options must be chosen before the player character")
    name = bounded_text(name, "name", MAX_NAME)
    pronouns = bounded_text(pronouns, "pronouns", MAX_NAME)
    _ensure_free_name(state, name)
    chosen = submitted.get("stats", {})
    rolled_stats = {s.id for s in spec.stats.values() if s.derived is None}
    if set(chosen) != rolled_stats:
        raise InvalidInput("every stat needs a value")
    try:
        check_swaps(spec, rolled, chosen)
        check_choices(spec, submitted)
        sheet = resolve(spec, submitted, rng, plus=plus)
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    _check_sheet_text(sheet)
    fields = _sheet_fields(sheet)
    starting = fields["features"]
    carried = _take_starting_items(spec, starting, inventory, item_sizes, magic, currency)
    record(db, campaign_id, PcAdded(name=name, pronouns=pronouns, **fields))
    # A rule set whose armor is its defense gives an unarmored character a base AC, such as
    # 10 + Dexterity. Focus and other feature effects add to the armor the carried features give.
    armor = defense_base
    if defense_modifier:
        armor += spec.modifier(sheet.stats, defense_modifier)
    armor += sheet.armor
    equipped_armor, equipped_shield, item_armor = _carry_starting_items(
        db,
        campaign_id,
        name,
        carried,
        inventory,
        item_sizes,
        magic,
        defense=bool(combat_rules is not None and combat_rules.defense_base),
    )
    armor += item_armor
    if combat_rules is not None and combat_rules.defense_base:
        created = _character_named(load_state(db, campaign_id), name)
        combat.set_defense_equipment(
            db,
            campaign_id,
            created,
            spec,
            combat_rules,
            armor=equipped_armor,
            shield=equipped_shield,
            adjustment=sheet.armor,
        )
    elif armor:
        cap = armor_max or combat.UNCAPPED_ARMOR
        record(db, campaign_id, ArmorSet(name=name, armor=min(armor, cap)))
    record(db, campaign_id, OpeningStarted())
    return {"created": "pc", "name": name, "location": None}


def create_location(
    db: Session,
    campaign_id: int,
    name: str,
    description: str,
    discovered: bool = False,
    x: float | None = None,
    y: float | None = None,
    force: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    _ensure_no_same_place(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    px, py = _coordinates(x, y)
    inside = None
    if parent is not None:
        inside = _parent_for_new_place(state, parent, px, py)
        px, py = None, None
    else:
        _ensure_on_terrain(state, name, px, py)
        _ensure_open_point(state, name, px, py, force)
    record(
        db,
        campaign_id,
        LocationAdded(
            name=name, description=text, discovered=discovered, x=px, y=py, parent=inside
        ),
    )
    db.commit()
    return {"created": "location", "name": name, "discovered": discovered}


def _ensure_on_terrain(state: CampaignState, name: str, x: float | None, y: float | None) -> None:
    """A place with a map point must sit inside a region with ground around it, so the map shows
    the terrain under the place rather than blank space."""
    if x is None or y is None:
        return
    ground = [region.points for region in state.regions.values()]
    if clearance((x, y), ground) < REGION_BUFFER_KM:
        raise InvalidInput(
            f"{name!r} is not on the map's terrain. Create a region that covers the place and "
            f"extends at least {REGION_BUFFER_KM} km beyond it, using create_region, then add "
            "the location."
        )


def create_region(
    db: Session,
    campaign_id: int,
    kinds: Mapping[str, WorldTerrain],
    name: str,
    terrain: str,
    points: Sequence[Sequence[float]],
    force: bool = False,
    discovered: bool = False,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    if name_key(name) in state.regions:
        raise InvalidInput(f"there is already a region named {name!r}")
    if terrain not in kinds:
        choices = ", ".join(sorted(kinds)) or "none"
        raise InvalidInput(f"{terrain!r} is not a terrain kind. The terrain kinds are: {choices}.")
    corners = _region_corners(points)
    problem = polygon_problem(corners)
    if problem is not None:
        raise InvalidInput(
            f"the region {problem}. Give its corners in order around the edge, in kilometers "
            "from the map's center."
        )
    if not isinstance(force, bool):
        raise InvalidInput("force must be true or false")
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    result: dict[str, Any] = {
        "created": "region",
        "name": name,
        "terrain": terrain,
        "discovered": discovered,
    }
    if not force:
        covering = [region for region in state.regions.values() if overlaps(corners, region.points)]
        if covering:
            # Ground already there is taken away, so the new region covers only open ground.
            cut = uncovered(corners, [region.points for region in state.regions.values()])
            if not cut.parts:
                names = ", ".join(repr(region.name) for region in covering)
                if cut.holed:
                    raise InvalidInput(
                        f"cutting {name!r} to fit the ground already there would leave a hole in "
                        f"it, because {names} lies wholly inside the outline. Draw around that "
                        "ground, or pass force=true to overlap it."
                    )
                raise InvalidInput(
                    f"{name!r} lies entirely on ground that is already covered, by {names}. Give "
                    "an outline that reaches open ground, or pass force=true to overlap it."
                )
            corners = cut.parts[0]
            result["points"] = [[x, y] for x, y in corners]
            left_out = len(cut.parts) - 1 + cut.holed
            if left_out:
                result["left_out"] = left_out
    record(
        db,
        campaign_id,
        RegionAdded(
            name=name,
            terrain=terrain,
            points=[[x, y] for x, y in corners],
            discovered=discovered,
        ),
    )
    db.commit()
    return result


def _region_corners(points: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    if not isinstance(points, list | tuple) or len(points) > MAX_REGION_CORNERS:
        raise InvalidInput(f"points must be a list of at most {MAX_REGION_CORNERS} [x, y] corners")
    corners: list[tuple[float, float]] = []
    for point in points:
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or any(isinstance(value, bool) or not isinstance(value, int | float) for value in point)
        ):
            raise InvalidInput(
                "each point must be an [x, y] pair of numbers, in kilometers from the map's center"
            )
        corners.append((float(point[0]), float(point[1])))
    return corners


def create_npc(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    name: str,
    location: str,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    *,
    monsters: Mapping[str, MonsterTemplate],
    force: bool = False,
    plus: str,
    npc_hp: Mapping[str, int],
    npc_hp_stat: str | None,
    hp_tier: str | None = None,
    pronouns: str | None = None,
    important: bool = False,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    try:
        check_choices(spec, character or {})
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    base = None if force else _statblock_named(monsters, name)
    if base is not None:
        if (character or {}).get("stats"):
            raise InvalidInput(_statblock_conflict(base, spec))
        character = {**(character or {}), "stats": dict(base.stats)}
    if hp_tier is not None:
        character = _with_hp_tier(
            character, hp_tier, npc_hp, npc_hp_stat, statblock=base is not None
        )
    event = _npc_event(
        tables,
        rng,
        spec,
        name=name,
        location=where.name,
        description=bounded_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
        character=character,
        plus=plus,
        pronouns=_optional_text(pronouns, "pronouns"),
        important=important,
    )
    if base is not None:
        event = _with_statblock(event, base)
    record(db, campaign_id, event)
    return {
        "created": "npc",
        "name": event.name,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
        "pronouns": event.pronouns,
        "statblock": base.id if base else None,
    }


def hire_hireling(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    name: str,
    employer: str,
    wage: int,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    pronouns: str | None = None,
    *,
    plus: str,
) -> dict[str, Any]:
    """Hire a character into the employer's company, at the employer's location. The hireling is
    controlled by the player and its wage is paid each day from the employer's currency. Its sheet is
    rolled the way an NPC's is."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    boss = state.characters.get(name_key(employer))
    if boss is None or boss.kind != PC_KIND or boss.hireling:
        raise InvalidInput(f"{employer} is not a player character who can hire")
    if boss.location is None:
        raise InvalidInput(f"{boss.name} has no location yet, so cannot hire anyone")
    if not isinstance(wage, int) or isinstance(wage, bool) or wage < 0:
        raise InvalidInput("the wage must be a whole number of currency, zero or more")
    try:
        check_choices(spec, character or {})
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    event = _npc_event(
        tables,
        rng,
        spec,
        name=name,
        location=boss.location,
        description=bounded_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
        character=character,
        plus=plus,
        pronouns=_optional_text(pronouns, "pronouns"),
    )
    record(
        db,
        campaign_id,
        HirelingAdded(
            name=event.name,
            employer=boss.name,
            wage=wage,
            location=event.location,
            description=event.description,
            personality=event.personality,
            accent=event.accent,
            quirks=list(event.quirks),
            pronouns=event.pronouns,
            stats=dict(event.stats),
            templates=dict(event.templates),
            features=dict(event.features),
            fields=dict(event.fields),
            counters=dict(event.counters),
            armor=event.armor,
            skills=dict(event.skills),
        ),
    )
    return {
        "hired": event.name,
        "employer": boss.name,
        "wage": wage,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
    }


def dismiss_hireling(db: Session, campaign_id: int, name: str, cause: str) -> dict[str, Any]:
    """The hireling leaves its employer's company and stays where it is, as an NPC."""
    state = load_state(db, campaign_id)
    hireling = state.characters.get(name_key(name))
    if hireling is None or not hireling.hireling:
        raise InvalidInput(f"{name} is not a hireling")
    cause = bounded_text(cause, "cause", MAX_TEXT)
    employer = hireling.employer
    record(db, campaign_id, HirelingDismissed(name=hireling.name, cause=cause))
    db.commit()
    return {"dismissed": hireling.name, "employer": employer}


def promote_hireling(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    """After the player character died, a living hireling takes over as the player character."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character is still alive")
    hireling = state.characters.get(name_key(name))
    if hireling is None or not hireling.hireling:
        raise InvalidInput(f"{name} is not a hireling")
    if hireling.dead or hireling.dying_hours is not None or hireling.dying_rounds is not None:
        raise InvalidInput(f"{hireling.name} cannot take up the part: they are not able to act")
    employer = hireling.employer
    record(db, campaign_id, HirelingPromoted(name=hireling.name, employer=employer))
    db.commit()
    return {"promoted": hireling.name, "employer": employer}


def create_monster(
    db: Session,
    campaign_id: int,
    monsters: Mapping[str, MonsterTemplate],
    spec: CharacterSpec,
    name: str,
    location: str,
    description: str | None,
    template: str | None,
    stats: Mapping[str, int | None],
    armor: int | None,
    attacks: Sequence[Mapping[str, Any]] | None,
    armor_max: int = 0,
    force: bool = False,
    detachment: bool | None = None,
    rules: Sequence[str] | None = None,
) -> dict[str, Any]:
    """An NPC built from a statblock. A template gives the starting values, and any value given
    replaces it. Without a template, a name that matches a statblock uses it and takes no values
    of its own, unless force is set. A detachment flag overrides the statblock's group setting;
    without it, the statblock decides."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    base: MonsterTemplate | None = None
    given = {key: value for key, value in stats.items() if value is not None}
    if template is not None:
        base = monsters.get(template.strip())
        if base is None:
            raise InvalidInput(
                f"there is no monster template {template!r}; search_monster lists them"
            )
    elif not force:
        base = _statblock_named(monsters, name)
        if base is not None and (given or armor is not None or attacks is not None):
            raise InvalidInput(_statblock_conflict(base, spec))
    merged = {**(base.stats if base else {}), **given}
    unknown = [key for key in merged if key not in spec.stats]
    if unknown:
        raise InvalidInput("unknown stats: " + ", ".join(unknown))
    missing = [
        spec.stats[key].name
        for key in spec.stats
        if key not in merged and spec.stats[key].derived is None
    ]
    if missing:
        raise InvalidInput("missing stats: " + ", ".join(missing))
    for key, value in merged.items():
        whole_number(value, f"{spec.stats[key].name}", 0, MAX_STAT)
    if description is not None:
        text = description
    elif base is not None:
        text = base.description
    else:
        raise InvalidInput("a description is needed when there is no template")
    armor_value = whole_number(
        armor if armor is not None else (base.armor if base else 0),
        "armor",
        0,
        armor_max or combat.UNCAPPED_ARMOR,
    )
    attack_map, flag_map, shock_map = _attack_table(attacks, base)
    event = NpcAdded(
        name=name,
        location=where.name,
        description=bounded_text(text, "description", MAX_TEXT),
        personality="",
        accent="",
        quirks=[],
        stats=dict(merged),
        armor=armor_value,
        attacks=attack_map,
        attack_flags=flag_map,
        attack_shock=shock_map,
        creature=base.name if base else "",
        rules=list(rules) if rules is not None else (list(base.rules) if base else []),
        large_group=(detachment if detachment is not None else bool(base and base.large_group)),
    )
    record(db, campaign_id, event)
    db.commit()
    return {
        "created": "monster",
        "name": event.name,
        "location": event.location,
        "template": base.id if base else None,
        "stats": dict(merged),
        "armor": armor_value,
        "attacks": attack_map,
        "attack_flags": flag_map,
        "attack_shock": shock_map,
        "detachment": event.large_group,
    }


def _attack_table(
    attacks: Sequence[Mapping[str, Any]] | None, base: MonsterTemplate | None
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[int]]]:
    """The attacks an NPC gets: the given ones, else the statblock's, else none. Returns the dice by
    attack name, the flags of the attacks that have any, and the Shock and the armor it shocks
    through, by attack name."""
    if attacks is not None:
        return _attacks(attacks)
    if base is None:
        return {}, {}, {}
    return (
        dict(base.attacks),
        {name: list(flags) for name, flags in base.attack_flags.items()},
        {name: list(pair) for name, pair in base.attack_shock.items()},
    )


def _attacks(
    attacks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[int]]]:
    """Each attack is {"name", "dice", "flags", "shock", "shock_ac"}. Its dice must be a formula
    such as '1d8' or '1d6+1d6'; flags is an optional list of names from ATTACK_FLAGS, each listed
    once; and shock and shock_ac say what it does to a target it misses whose armor is low enough.
    A Shock value needs a shock_ac. Returns the dice, the flags, and the shock of the attacks that
    have any."""
    checked: dict[str, str] = {}
    flags_by_attack: dict[str, list[str]] = {}
    shock_by_attack: dict[str, list[int]] = {}
    seen: set[str] = set()
    for attack in attacks:
        label = bounded_text(attack.get("name", ""), "attack name", MAX_NAME)
        dice = attack.get("dice", "")
        flags = attack.get("flags", [])
        shock = attack.get("shock", 0)
        shock_ac = attack.get("shock_ac", 0)
        if label.casefold() in seen:
            raise InvalidInput(f"attack {label!r} is listed twice")
        if not isinstance(dice, str) or not is_formula(dice):
            raise InvalidInput(f"attack {label!r} needs a dice formula such as '1d8' or '1d6+1d6'")
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise InvalidInput(f"attack {label!r} flags must be a list of names")
        unknown = [flag for flag in flags if flag not in ATTACK_FLAGS]
        if unknown or len(set(flags)) != len(flags):
            options = ", ".join(ATTACK_FLAGS)
            raise InvalidInput(f"attack {label!r} flags must each be listed once, from: {options}")
        shock_value = whole_number(shock, f"attack {label!r} shock", 0, MAX_STAT)
        shock_ac_value = whole_number(shock_ac, f"attack {label!r} shock_ac", 0, MAX_STAT)
        if shock_value and not shock_ac_value:
            raise InvalidInput(
                f"attack {label!r} gives Shock, so it needs the AC it shocks through"
            )
        seen.add(label.casefold())
        checked[label] = dice
        if flags:
            flags_by_attack[label] = list(flags)
        if shock_value:
            shock_by_attack[label] = [shock_value, shock_ac_value]
    return checked, flags_by_attack, shock_by_attack


def _statblock_conflict(base: MonsterTemplate, spec: CharacterSpec) -> str:
    """The refusal when a named statblock creature is given values of its own."""
    stats = ", ".join(
        f"{spec.stats[key].name} {value}" for key, value in base.stats.items() if key in spec.stats
    )
    attacks = "".join(
        f"; {label} {dice}{_flag_text(base.attack_flags.get(label, ()))}"
        for label, dice in base.attacks.items()
    )
    return (
        f"{base.name} has a published statblock ({stats}{attacks}). "
        "Leave out the values you would give to use it, or set force to create a custom "
        "creature with this name."
    )


def _with_statblock(event: NpcAdded, base: MonsterTemplate) -> NpcAdded:
    """The NPC event with a statblock's armor, attacks, creature, large-group flag, and rules.
    Its stats are set by the caller, before the event is made, so they are not rolled."""
    return replace(
        event,
        armor=base.armor,
        attacks=dict(base.attacks),
        attack_flags={name: list(flags) for name, flags in base.attack_flags.items()},
        attack_shock={name: list(pair) for name, pair in base.attack_shock.items()},
        creature=base.name,
        large_group=base.large_group,
        rules=list(dict.fromkeys([*event.rules, *base.rules])),
    )


def _with_hp_tier(
    character: Mapping[str, Any] | None,
    hp_tier: str,
    npc_hp: Mapping[str, int],
    stat: str | None,
    *,
    statblock: bool,
) -> dict[str, Any]:
    """The character with the HP of a tier. A tier is for an NPC with no statblock, and it sets
    the rule set's HP stat outright, so it cannot be combined with that stat given another way."""
    if stat is None or hp_tier not in npc_hp:
        choices = ", ".join(sorted(npc_hp)) or "none"
        raise InvalidInput(f"Unknown HP tier {hp_tier!r}; choose one of: {choices}")
    if statblock:
        raise InvalidInput(
            "A creature with a statblock takes its HP from the statblock; leave out hp_tier"
        )
    stats = dict((character or {}).get("stats") or {})
    if stat in stats:
        raise InvalidInput(f"Give either hp_tier or {stat}, not both")
    return {**(character or {}), "stats": {**stats, stat: npc_hp[hp_tier]}}


def _statblock_named(monsters: Mapping[str, MonsterTemplate], name: str) -> MonsterTemplate | None:
    """The statblock whose name or id is `name`, ignoring case, so a named module or bestiary
    creature gets its published stats without the caller supplying them."""
    wanted = name.strip().lower()
    slug = wanted.replace(" ", "-")
    for monster in monsters.values():
        if monster.id.lower() in (wanted, slug) or monster.name.lower() == wanted:
            return monster
    return None


def search_monster(
    monsters: Mapping[str, MonsterTemplate], query: str
) -> dict[str, list[dict[str, Any]]]:
    """Statblocks whose id, name, or description contain every word of `query`."""
    words = _words(query)
    matches = [
        {
            "id": monster.id,
            "name": monster.name,
            "description": monster.description,
            "stats": dict(monster.stats),
            "armor": monster.armor,
            "attacks": dict(monster.attacks),
            "attack_flags": {name: list(flags) for name, flags in monster.attack_flags.items()},
            "attack_shock": {name: list(pair) for name, pair in monster.attack_shock.items()},
        }
        for monster in monsters.values()
        if _has_words(words, monster.id, monster.name, monster.description)
    ]
    return {"monsters": matches}


def search_marketplace(marketplace: Marketplace, query: str) -> dict[str, Any]:
    """Catalogue items and services whose id, name, category, or description contain every word
    of `query`. Items come back in full, up to a limit, and `more` counts the rest."""
    words = _words(query)
    items = [
        {
            "item": item.name,
            "category": item.category,
            "price": item.price,
            "size": item.size,
            "description": item.description,
        }
        for item in marketplace.items
        if _has_words(words, item.id, item.name, item.category, item.description)
    ]
    services = [
        {"service": service.id, "name": service.name, "price": service.price}
        for service in marketplace.services
        if _has_words(words, service.id, service.name)
    ]
    return {
        "currency": marketplace.currency,
        "items": items[:MAX_MARKET_RESULTS],
        "more": max(0, len(items) - MAX_MARKET_RESULTS),
        "services": services,
    }


def update_location(
    db: Session,
    campaign_id: int,
    name: str,
    description: str,
    x: float | None = None,
    y: float | None = None,
    off_map: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Change a location's description and its map position. Giving x and y moves it. Giving
    off_map takes it off the map. Without any of these the position stays as it is."""
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(off_map, bool):
        raise InvalidInput("off_map must be true or false")
    px, py = _coordinates(x, y)
    if where.parent is not None:
        if px is not None or py is not None:
            raise InvalidInput(f"{where.name} is inside {where.parent}, so it has no map position")
        off_map = False
    if off_map:
        if px is not None or py is not None:
            raise InvalidInput("give x and y to move the place, or off_map to take it off the map")
    elif px is None and py is None and where.position is not None:
        px, py = where.position.x, where.position.y
    _ensure_open_point(state, where.name, px, py, force)
    record(db, campaign_id, LocationUpdated(name=where.name, description=text, x=px, y=py))
    db.commit()
    return {"updated": "location", "name": where.name}


def discover_location(
    db: Session, campaign_id: int, name: str, summary: str | None = None
) -> dict[str, Any]:
    """Discover a place. A given summary is what the player sees of it. Giving one for a place that
    is already discovered replaces its summary. A summary is never required."""
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = _optional_text(summary, "summary", MAX_SUMMARY)
    if not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name, summary=text))
        db.commit()
    elif text is not None and text != where.summary:
        record(db, campaign_id, LocationSummarized(name=where.name, summary=text))
        db.commit()
    return {"discovered": "location", "name": where.name}


def delete_location(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    occupants = [c.name for c in state.characters.values() if c.location == where.name]
    if occupants:
        raise InvalidInput(
            f"{where.name} still has {', '.join(occupants)} there; move them elsewhere first"
        )
    inside = _children_of(state, where.name)
    if inside:
        raise InvalidInput(
            f"{where.name} still has {', '.join(inside)} inside it; delete those places first"
        )
    mapped = [m.name for m in state.maps.values() if name_key(m.location) == name_key(where.name)]
    if mapped:
        raise InvalidInput(
            f"{where.name} still has the encounter map {', '.join(mapped)}; delete the map first"
        )
    record(db, campaign_id, LocationRemoved(name=where.name))
    db.commit()
    return {"deleted": "location", "name": where.name}


def update_npc(
    db: Session,
    campaign_id: int,
    spec: CharacterSpec,
    rng: random.Random,
    name: str,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    armor: int | None = None,
    attacks: Sequence[Mapping[str, Any]] | None = None,
    *,
    armor_max: int = 0,
    defense_base: int = 0,
    plus: str,
) -> dict[str, Any]:
    """Change the NPC's details. Only the given fields change; quirks, if given, replace the list.
    A story-only NPC can be given its stats once, with character, armor, and attacks."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    text_given = any(value is not None for value in (description, personality, accent, quirks))
    stats_given = any(value is not None for value in (character, armor, attacks))
    if not text_given and not stats_given:
        raise InvalidInput(
            "give at least one of description, personality, accent, quirks, or stats"
        )
    statted: NpcStatted | None = None
    if stats_given:
        if not npc.story_only:
            raise InvalidInput(
                f"{npc.name} already has stats; only a story-only NPC can be given them"
            )
        try:
            check_choices(spec, character or {})
            sheet = resolve(spec, character or {}, rng, plus=plus)
        except CharacterError as error:
            raise InvalidInput(str(error)) from error
        _check_sheet_text(sheet)
        attack_map, attack_flags, attack_shock = _attacks(attacks or [])
        statted = NpcStatted(
            name=npc.name,
            armor=(sheet.armor if defense_base else 0)
            if armor is None
            else whole_number(armor, "armor", 0, armor_max or combat.UNCAPPED_ARMOR),
            attacks=attack_map,
            attack_flags=attack_flags,
            attack_shock=attack_shock,
            **_sheet_fields(sheet),
        )
    if text_given:
        record(
            db,
            campaign_id,
            NpcUpdated(
                name=npc.name,
                description=npc.description
                if description is None
                else bounded_text(description, "description", MAX_TEXT),
                personality=npc.personality
                if personality is None
                else bounded_text(personality, "personality", MAX_NAME),
                accent=npc.accent if accent is None else bounded_text(accent, "accent", MAX_NAME),
                quirks=list(npc.quirks) if quirks is None else _quirks(quirks) or [],
            ),
        )
    if statted is not None:
        record(db, campaign_id, statted)
    return {"updated": "npc", "name": npc.name, "statted": statted is not None}


def update_npc_knowledge(
    db: Session,
    campaign_id: int,
    name: str,
    *,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    pronouns: str | None = None,
    relationship: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Record what the player character has learned of an NPC. Only the given fields change. The
    NPC need not have been met yet: a rumor can put someone in the player's mind, with whatever
    was told. `location` is the place the player now knows the NPC to be."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if all(
        value is None
        for value in (description, personality, accent, quirks, pronouns, relationship, location)
    ):
        raise InvalidInput(
            "give at least one of description, personality, accent, quirks, pronouns, "
            "relationship, or location"
        )
    known = state.known.get(name_key(npc.name), KnownNpc(name=npc.name))
    where = known.location
    if location is not None:
        where = _place_named(state, location).name
    record(
        db,
        campaign_id,
        NpcKnown(
            name=npc.name,
            description=known.description
            if description is None
            else bounded_text(description, "description", MAX_TEXT),
            personality=known.personality
            if personality is None
            else bounded_text(personality, "personality", MAX_NAME),
            accent=known.accent if accent is None else bounded_text(accent, "accent", MAX_NAME),
            quirks=list(known.quirks) if quirks is None else _quirks(quirks) or [],
            pronouns=known.pronouns if pronouns is None else _optional_text(pronouns, "pronouns"),
            relationship=known.relationship
            if relationship is None
            else bounded_text(relationship, "relationship", MAX_NAME),
            location=where,
        ),
    )
    db.commit()
    return {"updated": "npc knowledge", "name": npc.name}


def delete_npc(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if state.encounter is not None and npc.name in state.encounter.order:
        raise InvalidInput(f"{npc.name} is in the encounter; remove them from the encounter first")
    record(db, campaign_id, NpcRemoved(name=npc.name))
    db.commit()
    return {"deleted": "npc", "name": npc.name}


def move_actor(
    db: Session,
    campaign_id: int,
    name: str,
    location: str | None = None,
    *,
    travel: Travel | None = None,
    speed: str | None = None,
    mode: str = IN_SETTLEMENT,
    clock_rules: ClockRules | None = None,
    hooks: Hooks | None = None,
    inventory: InventoryRules | None = None,
) -> dict[str, Any]:
    """Move a character to a place. With no place, an NPC goes into the wind: it is nowhere
    in particular until a later move brings it back. A player character's move in a settlement
    takes time from the travel table at the given speed, when the rule set has one. Moves in a
    dungeon, and overland moves placed after their watches, take no clock time here. The clock
    advances by whole hours; the rest of the minutes carry to the next move."""
    state = load_state(db, campaign_id)
    actor = _character_named(state, name)
    if location is None:
        if actor.kind != NPC_KIND:
            raise InvalidInput(f"{actor.name} is the player character and cannot go into the wind")
        record(db, campaign_id, ActorMoved(name=actor.name, location=None))
        db.commit()
        return {"moved": actor.name, "location": "in the wind"}
    if speed is None:
        speed = travel.default_mode if travel is not None else ""
    if travel is not None and speed not in travel.modes:
        raise InvalidInput(
            f"there is no travel mode named {speed!r}; use one of {', '.join(sorted(travel.modes))}"
        )
    where = _place_named(state, location)
    minutes = None
    if travel is not None and actor.kind == PC_KIND and mode == IN_SETTLEMENT:
        minutes = _travel_minutes(state, actor, where, travel, speed)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    # Going somewhere in person means the player character knows the place in detail, and every
    # place that holds it: anyone inside the great hall is inside the castle too.
    if actor.kind == PC_KIND:
        for place in _enclosing_places(state, where):
            if not place.discovered:
                record(db, campaign_id, LocationDiscovered(name=place.name))
    # Hirelings travel with the player character who employs them.
    if actor.kind == PC_KIND and not actor.hireling:
        for hireling in state.characters.values():
            if hireling.hireling and hireling.employer == actor.name:
                record(db, campaign_id, ActorMoved(name=hireling.name, location=where.name))
    result: dict[str, Any] = {"moved": actor.name, "location": where.name}
    if minutes is not None and clock_rules is not None:
        result.update(
            _charge_travel(db, campaign_id, state, minutes, clock_rules, hooks, inventory, speed)
        )
    db.commit()
    return result


def _travelling_party(state: CampaignState, actor: Character) -> list[Character]:
    """The character and the hirelings who go with them, in that order."""
    if actor.hireling:
        return [actor]
    hirelings = [c for c in state.characters.values() if c.hireling and c.employer == actor.name]
    return [actor, *hirelings]


def _travel_minutes(
    state: CampaignState, actor: Character, where: Place, travel: Travel, mode: str
) -> int:
    """The whole minutes a move takes: the straight-line distance from where the actor is, at the
    mode's speed. A move with no distance to work out takes the default leg."""
    origin = _place_position(state, actor.location)
    destination = _place_position(state, where.name)
    if origin is None or destination is None:
        return travel.default_leg_minutes
    distance = math.hypot(destination[0] - origin[0], destination[1] - origin[1])
    return round(distance / travel.modes[mode].km_per_hour * 60)


def _place_position(state: CampaignState, name: str | None) -> tuple[float, float] | None:
    """The map position of a place, as kilometers (x, y). A place inside another has none of its
    own, so it is at its parent's position."""
    place = state.places.get(name_key(name)) if name else None
    if place is None:
        return None
    if place.position is None and place.parent is not None:
        place = state.places.get(name_key(place.parent))
        if place is None:
            return None
    if place.position is None:
        return None
    return (place.position.x, place.position.y)


def _slug(name: str) -> str:
    """A place name as a location id writes it: "Village Gate" becomes "village-gate"."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def _authored_route(
    state: CampaignState, origin: Place | None, dest: Place
) -> Mapping[str, Any] | None:
    """The route the world authors from origin to dest, or None. A place inside another place
    uses its parent's routes, and a route to a place inside another leads to that parent."""
    origins = [origin]
    if origin is not None and origin.parent is not None:
        origins.append(state.places.get(name_key(origin.parent)))
    destinations = {_slug(dest.name), name_key(dest.name)}
    if dest.parent is not None:
        dest_parent = state.places.get(name_key(dest.parent))
        if dest_parent is not None:
            destinations |= {_slug(dest_parent.name), name_key(dest_parent.name)}
    for place in origins:
        if place is None:
            continue
        for key, route in place.routes.items():
            if key in destinations:
                return route
    return None


def _charge_travel(
    db: Session,
    campaign_id: int,
    state: CampaignState,
    minutes: int,
    clock_rules: ClockRules,
    hooks: Hooks | None,
    inventory: InventoryRules | None,
    mode: str,
) -> dict[str, Any]:
    total = state.travel_minutes + minutes
    hours, carried = divmod(total, 60)
    record(db, campaign_id, TravelCharged(minutes=carried))
    result: dict[str, Any] = {"minutes": minutes, "speed": mode}
    if hours:
        start = clock_service.absolute(
            state.clock, len(clock_rules.parts), clock_rules.hours_per_part
        )
        clock = clock_service.advance_to(
            db, campaign_id, clock_rules, start + hours, hooks, inventory
        )
        result["clock"] = clock
    return result


def add_note(
    db: Session, campaign_id: int, text: str, subject: str | None = None
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    text = bounded_text(text, "text", MAX_TEXT)
    subject_name: str | None = None
    label = _optional_text(subject, "subject")
    if label is not None and label.casefold() != CAMPAIGN_WIDE:
        entry = state.named(label)
        if entry is None:
            raise InvalidInput(f"there is no location or character named {label!r}")
        subject_name = entry.name
    note_id = record(db, campaign_id, NoteAdded(text=text, subject=subject_name))
    db.commit()
    return {
        "noted": True,
        "id": note_id,
        "subject": subject_name if subject_name is not None else CAMPAIGN_WIDE,
    }


def update_note(db: Session, campaign_id: int, note_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    _note_with_id(state, note_id)
    record(db, campaign_id, NoteUpdated(note_id=note_id, text=bounded_text(text, "text", MAX_TEXT)))
    db.commit()
    return {"updated": "note", "id": note_id}


def delete_note(db: Session, campaign_id: int, note_id: int) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    note = _note_with_id(state, note_id)
    record(db, campaign_id, NoteRemoved(note_id=note_id, text=note.text))
    db.commit()
    return {"deleted": "note", "id": note_id}


def search_notes(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Notes whose text or subject name contains every word of `query`, newest first."""
    words = _words(query)
    matches: list[dict[str, Any]] = []
    for note in reversed(state.notes):
        if _has_words(words, note.subject, note.text):
            matches.append({"id": note.id, "subject": note.subject, "text": note.text})
    return {"notes": matches}


def append_journal(db: Session, campaign_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    added = bounded_text(text, "text", MAX_TEXT).strip()
    if not added:
        raise InvalidInput("text must hold what to add to the journal")
    record(db, campaign_id, JournalAppended(text=added))
    db.commit()
    return {"added": "journal", "day": state.clock.day}


def replace_in_journal(
    db: Session, campaign_id: int, old_text: str, new_text: str
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    old = bounded_text(old_text, "old_text", MAX_TEXT)
    if not old.strip():
        raise InvalidInput("old_text must hold the passage to replace")
    new = (
        bounded_text(new_text, "new_text", MAX_TEXT).strip() if str(new_text or "").strip() else ""
    )
    count = state.journal.count(old)
    if count == 0:
        raise InvalidInput(
            "the journal has no passage matching old_text; view or search it for the exact text"
        )
    if count > 1:
        raise InvalidInput(f"old_text matches {count} places; quote more of it so it matches once")
    record(db, campaign_id, JournalReplaced(old_text=old, new_text=new))
    db.commit()
    return {"replaced": "journal", "deleted": new == ""}


def view_journal(
    state: CampaignState, start_line: int = 1, end_line: int | None = None
) -> dict[str, Any]:
    """Lines `start_line` to `end_line` of the journal, numbered from 1, at most JOURNAL_VIEW_LINES."""
    lines = _journal_lines(state)
    if end_line is not None and end_line < start_line:
        raise InvalidInput("end_line must be at least start_line")
    first = max(1, start_line)
    last = min(first if end_line is None else end_line, len(lines))
    last = min(last, first + JOURNAL_VIEW_LINES - 1)
    return {
        "total_lines": len(lines),
        "lines": [
            {"number": number, "text": lines[number - 1]} for number in range(first, last + 1)
        ],
    }


def search_journal(
    state: CampaignState, query: str, context: int = 1
) -> dict[str, list[dict[str, Any]]]:
    """The passages of the journal that best match `query`, in journal order. A passage is a
    matching line with `context` lines on either side, numbered as `from_line` to `to_line`.
    Matching is fuzzy: a query word matches a journal word that contains it or is spelled nearly
    the same."""
    words = re.findall(r"\w+", bounded_text(query, "query", MAX_TEXT).casefold())
    lines = _journal_lines(state)
    context = max(0, min(context, MAX_JOURNAL_CONTEXT))
    scored = [
        (score, index)
        for index, line in enumerate(lines)
        if (score := _fuzzy_score(words, line)) > 0
    ]
    best = sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:MAX_JOURNAL_RESULTS]
    ranges: list[list[int]] = []
    for _score, index in sorted(best, key=lambda pair: pair[1]):
        low, high = max(0, index - context), min(len(lines) - 1, index + context)
        if ranges and low <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], high)
        else:
            ranges.append([low, high])
    return {
        "passages": [
            {
                "from_line": low + 1,
                "to_line": high + 1,
                "text": "\n".join(lines[low : high + 1]),
            }
            for low, high in ranges
        ]
    }


def _journal_lines(state: CampaignState) -> list[str]:
    return state.journal.split("\n") if state.journal else []


def _fuzzy_score(words: Sequence[str], paragraph: str) -> int:
    """How many of the query words the paragraph matches."""
    found = re.findall(r"\w+", paragraph.casefold())
    return sum(
        1
        for word in words
        if any(word in candidate for candidate in found)
        or get_close_matches(word, found, n=1, cutoff=FUZZY_CUTOFF)
    )


def all_at_location(state: CampaignState, location: str | None) -> dict[str, Any]:
    """A location as the GM needs it: its description, the NPCs there, whether the player character
    is there, and the notes about the place or the NPCs in it. Without a location, it is the
    player character's location. Statblocks are left out; `get_statblock` returns them."""
    if location is None:
        pc = state.pc()
        if pc is None or pc.location is None:
            raise InvalidInput("the player character is not at a location yet; name a location")
        location = pc.location
    place = _place_named(state, location)
    npcs = _npcs_at(state, place.name)
    pc = state.pc()
    subjects = {place.name, *(npc.name for npc in npcs)}
    return {
        "location": _location_entry(place),
        "player_character_here": pc is not None and pc.location == place.name,
        "npcs": [_npc_summary(npc) for npc in npcs],
        "notes": [
            {"id": note.id, "subject": note.subject, "text": note.text}
            for note in reversed(state.notes)
            if note.subject in subjects
        ],
    }


def search_location(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Locations whose name or description contains every word of `query`."""
    words = _words(query)
    matches = [
        _location_entry(place)
        for place in state.places.values()
        if _has_words(words, place.name, place.description)
    ]
    return {"locations": matches}


def search_npc(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """NPCs anywhere in the world whose details contain every word of `query`."""
    words = _words(query)
    matches = [
        _npc_summary(npc)
        for npc in state.characters.values()
        if npc.kind == NPC_KIND and _has_words(words, *_npc_words(npc))
    ]
    return {"npcs": matches}


def lookup_index(state: CampaignState) -> dict[str, list[dict[str, Any]]]:
    """Every location and NPC by name, each with a one-sentence brief. The brief is enough to know
    what to look up; the full entry comes from the lookup tools."""
    locations = [
        {"name": place.name, "brief": brief(place.description)} for place in state.places.values()
    ]
    npcs = [
        {"name": npc.name, "location": npc.location, "brief": brief(npc.description)}
        for npc in state.characters.values()
        if npc.kind == NPC_KIND
    ]
    return {"locations": locations, "npcs": npcs}


def _required[T](value: T | None, what: str) -> T:
    if value is None:
        raise InvalidInput(f"this rule set does not track {what}")
    return value


def speaker_at_player(state: CampaignState, name: str) -> bool:
    """Whether the named NPC or hireling is at the player character's location."""
    npc = _character_named(state, name)
    pc = state.pc()
    return pc is not None and npc.location == pc.location


def _npc_event(
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    *,
    name: str,
    location: str,
    description: str,
    personality: str | None,
    accent: str | None,
    quirks: Sequence[str] | None,
    character: Mapping[str, Any] | None = None,
    plus: str,
    story_only: bool = False,
    important: bool = False,
    rules: Sequence[str] = (),
    pronouns: str | None = None,
) -> NpcAdded:
    """The event that adds an NPC. A trait or character option that is not given is picked at
    random, so the picked values are stored in the event and replays do not roll again. A
    story-only NPC gets no stats at all."""
    trait_personality = personality or rng.choice(tables["personality"]).value
    trait_accent = accent or rng.choice(tables["accent"]).value
    if quirks is None:
        pool = [option.value for option in tables["quirk"]]
        trait_quirks = rng.sample(pool, min(QUIRKS_PER_NPC, len(pool)))
    else:
        trait_quirks = list(quirks)
    sheet_fields: dict[str, Any] = {}
    if not story_only:
        try:
            sheet = resolve(spec, character or {}, rng, plus=plus)
        except CharacterError as error:
            raise InvalidInput(str(error)) from error
        _check_sheet_text(sheet)
        sheet_fields = _sheet_fields(sheet)
        sheet_fields["armor"] = sheet.armor
    return NpcAdded(
        name=name,
        location=location,
        description=description,
        personality=trait_personality,
        accent=trait_accent,
        quirks=trait_quirks,
        story_only=story_only,
        important=important,
        rules=list(rules),
        pronouns=pronouns,
        **sheet_fields,
    )


def _sheet_fields(sheet: Sheet) -> dict[str, Any]:
    return {
        "stats": dict(sheet.stats),
        "templates": dict(sheet.templates),
        "features": dict(sheet.features),
        "fields": dict(sheet.fields),
        "counters": dict(sheet.counters),
        "skills": dict(sheet.skills),
    }


def _check_sheet_text(sheet: Sheet) -> None:
    for value in sheet.fields.values():
        if len(value) > MAX_TEXT:
            raise InvalidInput(f"character text must be at most {MAX_TEXT} characters")


def _ensure_free_name(state: CampaignState, name: str) -> None:
    if state.named(name) is not None:
        raise InvalidInput(f"the name {name!r} is already used by a location or character")


def _ensure_no_same_place(state: CampaignState, name: str) -> None:
    """A new place may not repeat an existing one under a different article, such as "The Farming
    Path" for "Farming Path"; discover_location reveals the existing place instead."""
    bare = _without_article(name)
    for place in state.places.values():
        if _without_article(place.name) == bare:
            raise InvalidInput(
                f"there is already a location called {place.name!r}; "
                "use discover_location to reveal it"
            )


def _without_article(name: str) -> str:
    key = name_key(name)
    return key.removeprefix("the ")


def _enclosing_places(state: CampaignState, place: Place) -> list[Place]:
    """The place and the places that enclose it, nearest first."""
    chain = [place]
    parent = place.parent
    while parent is not None:
        outer = state.named(parent)
        if not isinstance(outer, Place) or any(outer is seen for seen in chain):
            break
        chain.append(outer)
        parent = outer.parent
    return chain


def _parent_for_new_place(
    state: CampaignState, parent: str, x: float | None, y: float | None
) -> str:
    """Check a place to be created inside another. Such a place has no point of its own, and only
    a top-level place can hold others."""
    if x is not None or y is not None:
        raise InvalidInput("a place inside another has no map position; leave out x and y")
    outer = _place_named(state, parent)
    if outer.parent is not None:
        raise InvalidInput(
            f"{outer.name} is itself inside {outer.parent}; place it inside {outer.parent} instead"
        )
    return outer.name


def _ensure_open_point(
    state: CampaignState, name: str, x: float | None, y: float | None, force: bool
) -> None:
    """Refuse a point that another place already has, unless the GM passes force. Places do not
    share a point by default, so their pins never sit on top of each other."""
    if not isinstance(force, bool):
        raise InvalidInput("force must be true or false")
    if x is None or y is None or force:
        return
    for other in state.places.values():
        if other.name == name or other.position is None:
            continue
        if abs(other.position.x - x) < SAME_POINT_KM and abs(other.position.y - y) < SAME_POINT_KM:
            raise InvalidInput(
                f"{other.name!r} is already at that point. Choose another point, or pass "
                "force=true to put both places there."
            )


def _npc_named(state: CampaignState, name: str) -> Character:
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND:
        raise InvalidInput(f"{npc.name} is the player character, not an NPC")
    return npc


def _note_with_id(state: CampaignState, note_id: int) -> Note:
    note = next((n for n in state.notes if n.id == note_id), None)
    if note is None:
        raise InvalidInput(f"there is no note with id {note_id}")
    return note


MAX_MARKET_RESULTS = 20
MAX_JOURNAL_RESULTS = 5
MAX_JOURNAL_CONTEXT = 3
JOURNAL_VIEW_LINES = 50
FUZZY_CUTOFF = 0.8


def _words(query: str) -> list[str]:
    return bounded_text(query, "query", MAX_TEXT).casefold().split()


def _has_words(words: Sequence[str], *parts: str) -> bool:
    haystack = " ".join(parts).casefold()
    return all(word in haystack for word in words)


def _optional_text(value: object, label: str, limit: int = MAX_NAME) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bounded_text(value, label, limit)


def _coordinates(x: object, y: object) -> tuple[float | None, float | None]:
    """Both map coordinates or neither. Neither means the place has no position on the map."""
    if x is None and y is None:
        return None, None
    if x is None or y is None:
        raise InvalidInput("give both x and y, or neither")
    return _kilometers(x, "x"), _kilometers(y, "y")


def _kilometers(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidInput(f"{label} must be a number of kilometers from the map's center")
    return float(value)


def _quirks(value: Sequence[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or len(value) > MAX_QUIRKS:
        raise InvalidInput(f"quirks must be a list of up to {MAX_QUIRKS} short texts")
    return [bounded_text(item, "each quirk", MAX_NAME) for item in value]
