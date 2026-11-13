"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import Engine
from sqlmodel import Session

from cygnusloom.content.character import (
    CharacterSpec,
    Feature,
    TableOption,
)
from cygnusloom.content.library import (
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
    FactionAssetDefinition,
    Hooks,
    HousekeepingRules,
    InventoryRules,
    JourneyRules,
    LevelUpRules,
    MagicRules,
    Marketplace,
    MonsterTemplate,
    MoveRules,
    PeriodRules,
    ProjectDefinition,
    RelationRules,
    RestLength,
    ShopItem,
    TallyRules,
    Travel,
    Wilderness,
    WorldTerrain,
)
from cygnusloom.content.lookup import AmbiguousId, resolve_id
from cygnusloom.rules.dice import is_formula, max_total, roll_total
from cygnusloom.services import advancements as advancements_service
from cygnusloom.services import aid, combat, custom_features, encounters, morale, reactions
from cygnusloom.services import attacks as attack_modifiers
from cygnusloom.services import checks as checks_service
from cygnusloom.services import clock as clock_service
from cygnusloom.services import counters as counters_service
from cygnusloom.services import factions as factions_service
from cygnusloom.services import inventory as inventory_service
from cygnusloom.services import magic as magic_service
from cygnusloom.services import market as market_service
from cygnusloom.services import moves as moves_service
from cygnusloom.services import objects as objects_service
from cygnusloom.services import periods as periods_service
from cygnusloom.services import relations as relations_service
from cygnusloom.services import rests as rests_service
from cygnusloom.services import tallies as tallies_service
from cygnusloom.services import wilderness as wilderness_service
from cygnusloom.services.common import (
    InvalidInput,
    bounded_text,
    session_scope,
    whole_number,
)
from cygnusloom.services.conditions import add_condition, end_condition
from cygnusloom.services.dice_tables import roll_creative as roll_creative_tables
from cygnusloom.services.dice_tables import roll_tables as roll_dice_tables
from cygnusloom.services.dungeon import dungeon_cycle as dungeon_cycle_service
from cygnusloom.services.dungeon import dungeon_rules
from cygnusloom.services.dungeon import roll_reaction as roll_reaction_service
from cygnusloom.services.events import (
    PC_KIND,
    CharacterSaved,
    CheckMade,
    CurrencyChanged,
    ExecutionPrepared,
    ExecutionTaken,
    FeatureChoiceMade,
    FeatureUsed,
    GrappleEnded,
    GrappleStarted,
    Harmed,
    Item,
    MaxChanged,
    MusicCued,
    NarrationUndone,
    ProjectAbandoned,
    ProjectAdvanced,
    ProjectStarted,
    ReactionUsed,
    Recovered,
    SaveRolled,
    SkillChanged,
    Surprised,
    TotalDefenseTaken,
    current,
    decode,
    events_since,
    load_state,
    name_key,
    record,
    same_item_name,
)
from cygnusloom.services.hooks import HookContext, call_hook
from cygnusloom.services.music import MOODS, scene_key
from cygnusloom.services.recovery import recover
from cygnusloom.services.views import (
    _character_named,
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


from cygnusloom.services.world_people import PeopleMixin
from cygnusloom.services.world_places import PlacesMixin
from cygnusloom.services.world_support import *
from cygnusloom.services.world_support import _initialize_generated_defense, _required
from cygnusloom.services.world_travel import TravelMixin


def _last_successful_save(db: Session, campaign_id: int, name: str, stat: str) -> Any:
    """The most recent save this character made against `stat`, if it succeeded."""
    for row in reversed(events_since(db, campaign_id)):
        event = decode(row)
        if event.kind not in (CharacterSaved.kind, SaveRolled.kind):
            continue
        if getattr(event, "stat", "") != stat:
            continue
        if name_key(getattr(event, "name", "")) != name_key(name):
            continue
        return event if event.passed else None
    return None


def _finish_reaction_check(pending: Mapping[str, Any], total: int) -> dict[str, Any]:
    """Apply the player's reroll to a check and return the result to record."""
    bonus = int(pending["bonus"])
    difficulty = int(pending["difficulty"])
    original_roll = int(pending["original_roll"])
    original_total = int(pending["original_total"])
    original_success = bool(pending["original_success"])
    if pending.get("effect") == "reroll_ones":
        final_roll = original_roll - len(pending.get("ones") or []) + total
        final_total = final_roll + bonus
        final_success = final_total >= difficulty
    else:
        new_total = total + bonus
        new_success = new_total >= difficulty
        keep = str(pending.get("keep", "better"))
        better = (new_success and not original_success) or (
            new_success == original_success and new_total > original_total
        )
        worse = (original_success and not new_success) or (
            new_success == original_success and new_total < original_total
        )
        if (keep == "better" and better) or (keep == "worse" and worse):
            final_roll, final_total, final_success = total, new_total, new_success
        else:
            final_roll, final_total, final_success = (
                original_roll,
                original_total,
                original_success,
            )
    return {
        "name": str(pending.get("name", "")),
        "check": str(pending.get("check", "Skill Check")),
        "skill": str(pending.get("skill", "")),
        "skill_name": str(pending.get("skill_name", "")),
        "skill_level": int(pending.get("skill_level", 0)),
        "stat": str(pending.get("stat", "")),
        "stat_name": str(pending.get("stat_name", "")),
        "roll": final_roll,
        "bonus": bonus,
        "total": final_total,
        "difficulty": difficulty,
        "success": final_success,
        "armor_penalty": int(pending.get("armor_penalty", 0)),
        "cause": str(pending.get("cause", "")),
    }


def _finish_reaction_save(pending: Mapping[str, Any], total: int) -> dict[str, Any]:
    """Apply the player's reroll to a save and return the result to record."""
    score = int(pending["score"])
    original_roll = int(pending["original_roll"])
    original_success = bool(pending["original_success"])
    new_success = total >= score
    keep = str(pending.get("keep", "better"))
    better = (new_success and not original_success) or (
        new_success == original_success and total > original_roll
    )
    worse = (original_success and not new_success) or (
        new_success == original_success and total < original_roll
    )
    final_roll = (
        total if (keep == "better" and better) or (keep == "worse" and worse) else original_roll
    )
    return {
        "name": str(pending.get("name", "")),
        "stat": str(pending.get("stat", "")),
        "roll": final_roll,
        "score": score,
        "passed": final_roll >= score,
        "cause": str(pending.get("cause", "")),
    }


@dataclass(frozen=True)
class WorldTools(PlacesMixin, PeopleMixin, TravelMixin):
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
    projects: Mapping[str, ProjectDefinition] = field(default_factory=dict)
    # The rule set's faction assets by id, such as Infantry or Smugglers.
    faction_assets: Mapping[str, FactionAssetDefinition] = field(default_factory=dict)
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)
    # The rule set's moves by id.
    moves: Mapping[str, MoveRules] = field(default_factory=dict)
    # The rule set's counters by id, such as Experience or Luck.
    counters: Mapping[str, CounterRules] = field(default_factory=dict)
    advancements: Mapping[str, AdvancementRules] = field(default_factory=dict)
    # The rule set's experience levels, or empty when characters do not gain levels.
    level_ups: Mapping[str, LevelUpRules] = field(default_factory=dict, kw_only=True)
    periods: Mapping[str, PeriodRules] = field(default_factory=dict)
    tallies: Mapping[str, TallyRules] = field(default_factory=dict)
    relations: Mapping[str, RelationRules] = field(default_factory=dict)
    # Time, rest, and the rule set's hooks. Each is None or empty when the rule set does not use it.
    clock_rules: ClockRules | None = None
    rests: Mapping[str, RestLength] = field(default_factory=dict)
    interrupted_rest: str = ""
    hooks: Hooks | None = None
    condition_ends: tuple[str, ...] = ()
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
    travel_modes: Travel | None = None
    # The rule set's whole-day journey model, or None when travel is not measured that way.
    journey: JourneyRules | None = None
    # The rule set's wilderness calendar, weather, and Wilderness Actions, or None when it has none.
    wilderness: Wilderness | None = None
    # The kinds of ground a world map's regions can be, by id.
    world_terrain: Mapping[str, WorldTerrain] = field(default_factory=dict)
    # What the GM should tidy at a place earlier scenes left behind, or None for none.
    housekeeping: HousekeepingRules | None = None

    def roll_tables(self, tables: list[str]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return roll_dice_tables(
                db, self.campaign_id, tables, self.dice_tables, self.rng, plus=self.plus
            )

    def list_dice_tables(self) -> dict[str, Any]:
        """The dice and creative tables this campaign offers, after genre filtering."""
        return {
            "tables": [
                {
                    "id": table.id,
                    "name": table.name,
                    "dice": table.dice,
                    "creative": table.creative,
                }
                for table in self.dice_tables.values()
            ]
        }

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

    def rest(
        self,
        length: str,
        interrupted: bool = False,
        reason: str = "",
        scope: str = "party",
        character: str = "",
        advance_clock: bool | None = None,
    ) -> dict[str, Any]:
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
                condition_ends=self.condition_ends,
                rule_data=self.rule_data,
                scope=scope,
                character=character,
                advance_clock=advance_clock,
            )

    def rest_history(self) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return rests_service.rest_history(db, self.campaign_id)

    def with_hook_context(self, action: Callable[[HookContext], T]) -> T:
        with session_scope(self.engine) as db:
            result = action(
                HookContext(db, self.campaign_id, self.hooks, self.inventory, self.rule_data)
            )
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

    def transfer_currency(self, payer: str, payee: str, amount: int, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.transfer_currency(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                payer,
                payee,
                amount,
                cause,
            )

    def roll_currency(
        self, name: str, dice: str, multiplier: int, cause: str = ""
    ) -> dict[str, Any]:
        """Give a character currency from a roll, such as starting money: the dice total times
        `multiplier`, in the rule set's currency. The result holds the amount under the
        currency's own key, such as `silver`."""
        if not is_formula(dice):
            raise InvalidInput(f"{dice!r} is not a dice formula such as '3d6'")
        factor = whole_number(multiplier, "multiplier", 1, MAX_STAT)
        amount = roll_total(dice, self.rng, plus=self.plus) * factor
        changed = self.change_currency(name, amount, cause)
        return {"character": changed["name"], str(changed["currency"]): amount}

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
        item_text: str = "",
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
                item_text,
            )

    def take_object(self, object_name: str, character: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return objects_service.take_object(
                db,
                self.campaign_id,
                self.marketplace,
                self.inventory,
                self.item_sizes,
                object_name,
                character,
                cause,
            )

    def buy_service(
        self, names: Sequence[str], service: str, price: int | None = None
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.buy_service(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                names,
                service,
                hooks=self.hooks,
                price=price,
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
            encumbrance_checks=relic.encumbrance_checks,
            icon=relic.icon or "",
            looks_like=relic.looks_like,
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

    def transfer_item(self, giver: str, receiver: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            result = inventory_service.transfer_item(
                db,
                self.campaign_id,
                _required(self.inventory, "inventory"),
                giver,
                receiver,
                item,
            )
            self._unequip_item(db, giver, item)
            db.commit()
            return result

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
                self.magic.charged.kind
                if self.magic is not None and self.magic.item is not None
                else "",
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
        if magic.item is None:
            raise InvalidInput("this rule set does not use item magic")
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
        self,
        move: str,
        character: str,
        modifier: int = 0,
        stat: str | None = None,
        roll: int | None = None,
        spell: str = "",
    ) -> dict[str, Any]:
        try:
            move_id = resolve_id(self.moves, move, kind="move", names=lambda rules: (rules.label,))
        except AmbiguousId as exc:
            raise InvalidInput(str(exc)) from exc
        except LookupError:
            raise InvalidInput(
                f"there is no move named {move!r}; choose one of {sorted(self.moves)}"
            ) from None
        with session_scope(self.engine) as db:
            return moves_service.make_move(
                db,
                self.campaign_id,
                character,
                self.moves[move_id],
                self.rng,
                plus=self.plus,
                combat=self.combat,
                conditions=self.conditions,
                counters=self.counters,
                modifier=modifier,
                stat=stat,
                spec=self.character,
                features=self.features,
                roll=roll,
                magic=self.magic,
                spell=spell,
            )

    def choose_move_option(self, move: str, band: str, choice: str) -> dict[str, Any]:
        """Pick one of the choices a move band offered the player after their roll."""
        with session_scope(self.engine) as db:
            return moves_service.choose_option(
                db,
                self.campaign_id,
                self.moves,
                move,
                band,
                choice,
                self.rng,
                plus=self.plus,
                combat=self.combat,
                conditions=self.conditions,
                counters=self.counters,
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

    def _armor_check_penalty(self, db: Session, character: str, skill: str) -> int:
        """What worn armor takes off a check for `skill`: its Encumbrance slots when the armor
        declares that check, such as medium armor on Sneak or heavy armor on Sneak and Exert."""
        skill = skill.strip()
        if not skill:
            return 0
        found = load_state(db, self.campaign_id).characters.get(name_key(character))
        if found is None:
            return 0
        penalty = 0
        for worn in (found.equipped_armor, found.equipped_shield):
            if not worn:
                continue
            listed = self._listed(worn, include_character=True)
            if listed is None or skill not in listed.encumbrance_checks:
                continue
            held = next((item for item in found.items if same_item_name(item.name, worn)), None)
            if held is not None:
                penalty -= held.slots
        return penalty

    def roll_check(
        self,
        check: str,
        character: str,
        skill: str = "",
        stat: str = "",
        difficulty: int | None = None,
        modifier: int = 0,
        cause: str = "",
        features: Mapping[str, Feature] | None = None,
        roll: int | None = None,
        dice_values: list[int] | None = None,
        use_optional: bool = True,
        player_rolls_reaction: bool = False,
    ) -> dict[str, Any]:
        try:
            check_id = resolve_id(
                self.checks, check, kind="check", names=lambda rules: (rules.label,)
            )
        except AmbiguousId as exc:
            raise InvalidInput(str(exc)) from exc
        except LookupError:
            options = ", ".join(sorted(self.checks)) or "none"
            raise InvalidInput(
                f"there is no check named {check!r}; the checks are: {options}"
            ) from None
        skill_id = checks_service.resolve_skill(self.character, skill)
        stat_id = checks_service.resolve_stat(self.character, stat)
        with session_scope(self.engine) as db:
            return checks_service.roll_check(
                db,
                self.campaign_id,
                character,
                self.checks[check_id],
                self.character,
                self.rng,
                plus=self.plus,
                skill=skill_id,
                stat=stat_id,
                difficulty=difficulty,
                modifier=modifier,
                cause=cause,
                conditions=self.conditions,
                armor_penalty=self._armor_check_penalty(db, character, skill_id),
                features=features if features is not None else self.features,
                roll=roll,
                dice_values=dice_values,
                use_optional=use_optional,
                player_rolls_reaction=player_rolls_reaction,
            )

    def optional_check_reactions(
        self, name: str, check: str, skill: str = "", stat: str = "", cause: str = ""
    ) -> list[str]:
        """The optional check reactions to offer on a player's roll prompt."""
        with session_scope(self.engine) as db:
            character = load_state(db, self.campaign_id).characters.get(name_key(name))
            rules = self.checks.get(check)
            if character is None or rules is None:
                return []
            return list(
                reactions.offered_check(
                    character, self.character, self.features, rules, skill, stat, cause
                )
            )

    def optional_save_reactions(self, name: str, stat: str, cause: str = "") -> list[str]:
        """The optional save reactions to offer on a player's roll prompt."""
        with session_scope(self.engine) as db:
            character = load_state(db, self.campaign_id).characters.get(name_key(name))
            if character is None:
                return []
            stat = magic_service.save_stat_id(self.character, stat)
            return list(
                reactions.offered_save(character, self.character, self.features, stat, cause)
            )

    def resolve_reaction(
        self, pending: Mapping[str, Any], total: int, dice: Sequence[int] = ()
    ) -> dict[str, Any]:
        """Finish a check or save reaction with the player's own reroll and record it."""
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(str(pending.get("name", ""))))
            if character is None:
                raise InvalidInput("the character with that reaction is no longer here")
            feature_id = str(pending.get("feature", ""))
            moved = custom_features.merged_features(self.character, character)
            feature = moved.get(feature_id) or self.features.get(feature_id)
            reaction = (
                next((item for item in feature.reactions if item.id == pending["reaction"]), None)
                if feature is not None
                else None
            )
            if feature is None or reaction is None:
                raise InvalidInput("that reaction is no longer available")
            triggered = reactions.Triggered(feature, reaction, str(pending.get("text", "")))
            if pending.get("kind") == "save":
                result = _finish_reaction_save(pending, total)
                record(
                    db,
                    self.campaign_id,
                    CharacterSaved(
                        name=character.name,
                        stat=str(pending["stat"]),
                        roll=int(result["roll"]),
                        score=int(result["score"]),
                        passed=bool(result["passed"]),
                        cause=str(pending.get("cause", "")),
                        reaction=triggered.text,
                    ),
                )
            else:
                result = _finish_reaction_check(pending, total)
                record(
                    db,
                    self.campaign_id,
                    CheckMade(
                        name=character.name,
                        check=str(pending.get("check", "Skill Check")),
                        skill=str(pending.get("skill", "")),
                        skill_name=str(pending.get("skill_name", "")),
                        skill_level=int(pending.get("skill_level", 0)),
                        skill_bonus=int(pending.get("skill_level", 0)),
                        stat=str(pending.get("stat", "")),
                        stat_name=str(pending.get("stat_name", "")),
                        roll=int(result["roll"]),
                        bonus=int(result["bonus"]),
                        total=int(result["total"]),
                        difficulty=int(result["difficulty"]),
                        success=bool(result["success"]),
                        cause=str(pending.get("cause", "")),
                        armor_penalty=int(pending.get("armor_penalty", 0)),
                        reaction=triggered.text,
                    ),
                )
            reactions.use(db, self.campaign_id, character, triggered)
            db.commit()
            result["reaction"] = triggered.text
            return result

    def raise_skill(self, name: str, skill: str, level: int, cause: str = "") -> dict[str, Any]:
        skill = checks_service.resolve_skill(self.character, skill)
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

    def harm_stat(self, name: str, stat: str, amount: int, cause: str) -> dict[str, Any]:
        """Add harm to a character's stat, never past its maximum: the amount is clamped to what
        is left, and a stat already at its maximum refuses any harm. Records the harm and returns
        what the stat has left and how much was taken."""
        value = whole_number(amount, "amount", 1, MAX_STAT)
        with session_scope(self.engine) as db:
            character = _character_named(load_state(db, self.campaign_id), name)
            if stat not in character.stats:
                options = ", ".join(sorted(character.stats)) or "none"
                raise InvalidInput(
                    f"{character.name} has no stat {stat!r}; the stats are: {options}"
                )
            available = current(character, stat)
            if available <= 0:
                raise InvalidInput(f"{character.name}'s {stat} is already at its maximum")
            taken = min(value, available)
            record(
                db,
                self.campaign_id,
                Harmed(
                    name=character.name,
                    stat=stat,
                    taken=taken,
                    cause=bounded_text(cause, "cause", MAX_TEXT),
                ),
            )
            db.commit()
            return {
                "character": character.name,
                stat: available - taken,
                "added": taken,
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

    def create_faction(
        self,
        name: str,
        description: str,
        trait: str,
        points: int = 0,
        tally: str = "",
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.create_faction(
                db,
                self.campaign_id,
                self.tallies,
                name,
                description,
                trait,
                points=points,
                tally=tally,
            )

    def update_faction(
        self,
        faction: str,
        name: str | None = None,
        description: str | None = None,
        trait: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.update_faction(
                db,
                self.campaign_id,
                faction,
                name=name,
                description=description,
                trait=trait,
            )

    def remove_faction(self, faction: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.remove_faction(db, self.campaign_id, faction)

    def adjust_faction_points(
        self,
        faction: str,
        change: int,
        reason: str = "",
        award: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.adjust_faction_points(
                db,
                self.campaign_id,
                self.tallies,
                faction,
                change,
                reason=reason,
                award=award,
                force=force,
            )

    def list_factions(self) -> list[dict[str, Any]]:
        with session_scope(self.engine) as db:
            return factions_service.list_factions(db, self.campaign_id, self.tallies)

    def add_faction_asset(
        self, faction: str, kind: str, name: str = "", notes: str = ""
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.add_faction_asset(
                db,
                self.campaign_id,
                self.faction_assets,
                faction,
                kind,
                name,
                notes,
            )

    def update_faction_asset(
        self,
        faction: str,
        asset: str,
        name: str | None = None,
        notes: str | None = None,
        qualities: list[str] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.update_faction_asset(
                db,
                self.campaign_id,
                faction,
                asset,
                name=name,
                notes=notes,
                qualities=qualities,
            )

    def remove_faction_asset(self, faction: str, asset: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.remove_faction_asset(db, self.campaign_id, faction, asset)

    def faction_asset_acted(self, faction: str, asset: str, note: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.faction_asset_acted(db, self.campaign_id, faction, asset, note)

    def damage_faction_asset(self, faction: str, asset: str, amount: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.damage_faction_asset(
                db, self.campaign_id, faction, asset, amount
            )

    def repair_faction_asset(self, faction: str, asset: str, amount: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.repair_faction_asset(
                db, self.campaign_id, faction, asset, amount
            )

    def take_faction_turn(self, faction: str, note: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return factions_service.take_faction_turn(db, self.campaign_id, faction, note)

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
                journey=self.journey,
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

    def empowered_sorcery(
        self, dice: str, original: int | None = None, character: str = ""
    ) -> dict[str, Any]:
        """Reroll a spell effect's variable dice through the Empowered Sorcery art. The caller
        chooses which total to use; the Effort is committed separately. `character` is the
        caster, defaulting to the player character so an NPC with the art can use it too."""
        if not is_formula(dice):
            raise InvalidInput(f"{dice!r} is not a dice formula such as '3d6'")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            caster = state.characters.get(name_key(character)) if character else state.pc()
            if caster is None:
                raise InvalidInput(f"there is no character named {character!r}")
            if caster.features.get("art_empowered_sorcery", 0) <= 0:
                raise InvalidInput(f"{caster.name} does not have Empowered Sorcery")
            rerolled = roll_total(dice, self.rng, plus=self.plus)
            took = (
                f"rerolls {dice} from {original} to {rerolled}"
                if original is not None
                else f"rerolls {dice} for {rerolled}"
            )
            record(
                db,
                self.campaign_id,
                FeatureUsed(
                    name=caster.name,
                    feature="art_empowered_sorcery",
                    feature_name="Empowered Sorcery",
                    text=took,
                ),
            )
            db.commit()
            return {
                "character": caster.name,
                "dice": dice,
                "original": original,
                "rerolled": rerolled,
            }

    def inexorable_effect(
        self, target: str, stat: str, reason: str = "", character: str = ""
    ) -> dict[str, Any]:
        """Force a reroll of a successful enemy save through the Inexorable Effect art. The
        caster defaults to the player character, so an NPC with the art can use it too."""
        spec = self.character
        stat = magic_service.save_stat_id(spec, stat)
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            caster = state.characters.get(name_key(character)) if character else state.pc()
            if caster is None:
                raise InvalidInput(f"there is no character named {character!r}")
            if caster.features.get("art_inexorable_effect", 0) <= 0:
                raise InvalidInput(f"{caster.name} does not have Inexorable Effect")
            enemy = state.characters.get(name_key(target))
            if enemy is None:
                raise InvalidInput(f"there is no character named {target!r}")
            if enemy.name == caster.name:
                raise InvalidInput("Inexorable Effect targets an enemy, not the caster")
            if enemy.story_only:
                raise InvalidInput(f"{enemy.name} has no stats yet and cannot save")
            if stat not in enemy.stats:
                raise InvalidInput(f"{enemy.name} has no stat {stat!r}")
            key = "hour:art_inexorable_effect:inexorable-effect"
            if caster.reaction_uses.get(key, 0) >= 1:
                raise InvalidInput("Inexorable Effect is already used this scene")
            previous = _last_successful_save(db, self.campaign_id, enemy.name, stat)
            if previous is None:
                raise InvalidInput("Inexorable Effect needs an enemy save that just succeeded")
            rules = _required(self.combat, "combat")
            result = combat.resolve_save(rules, current(enemy, stat), rng=self.rng, plus=self.plus)
            worse = min(int(previous.roll), result.roll)
            record(
                db,
                self.campaign_id,
                CharacterSaved(
                    name=enemy.name,
                    stat=stat,
                    roll=worse,
                    score=result.score,
                    passed=worse >= result.score,
                    cause=f"Inexorable Effect: {reason}".strip(),
                ),
            )
            record(
                db,
                self.campaign_id,
                ReactionUsed(
                    name=caster.name,
                    feature="art_inexorable_effect",
                    feature_name="Inexorable Effect",
                    reaction="inexorable-effect",
                    reset="hour",
                    text="forces an enemy's successful save to reroll.",
                ),
            )
            db.commit()
            return {
                "character": caster.name,
                "target": enemy.name,
                "stat": stat,
                "original": previous.roll,
                "rerolled": result.roll,
                "roll": worse,
                "score": result.score,
                "passed": worse >= result.score,
            }

    def roll_save(
        self,
        name: str,
        stat: str,
        reason: str,
        natural: int | None = None,
        use_optional: bool = True,
        player_rolls_reaction: bool = False,
    ) -> dict[str, Any]:
        stat = magic_service.save_stat_id(self.character, stat)
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
                features=self.features,
                use_optional=use_optional,
                player_rolls_reaction=player_rolls_reaction,
            )

    def place_names(self) -> list[str]:
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            return sorted(place.name for place in state.places.values())

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
        force: bool = False,
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
            return self.resolve_attacks(target, entries, force=force)
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
                force=force,
                cell_feet=self._cell_feet(),
            )

    def _cell_feet(self) -> int | None:
        """The encounter grid's cell size, which attack reach and range use."""
        return self.grid.cell_feet if self.grid is not None else None

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

    def resolve_attacks(
        self, target: str, attacks: Sequence[Mapping[str, str]], force: bool = False
    ) -> dict[str, Any]:
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
                force=force,
                cell_feet=self._cell_feet(),
            )

    def player_attack(
        self,
        target: str,
        weapon: str,
        modifier: str = "",
        attack_roll: int | None = None,
        damage_roll: int | None = None,
        hit_modifier: int = 0,
        range_band: str = "",
        dual_wield: bool = False,
        force: bool = False,
        character: str = "",
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
                range_band=range_band,
                dual_wield=dual_wield,
                force=force,
                character=character,
                cell_feet=self._cell_feet(),
            )

    def attack_dice(self, weapon: str, modifier: str = "") -> str:
        with session_scope(self.engine) as db:
            return combat.attack_dice(db, self.campaign_id, weapon, self._rules(), modifier)

    def resolve_group_break(self, name: str, outcome: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.resolve_group_break(db, self.campaign_id, name, outcome, self._rules())

    def modifier_dice(self, modifier: str) -> str | None:
        return attack_modifiers.modifier_dice(self._rules().modifiers, modifier)

    def player_save(self, roll: int = 0, stat: str = "", cause: str = "") -> dict[str, Any]:
        """Record the player's own d20 for a save the combat rules have marked due, or for a
        hazard save against the named stat."""
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
                stat=stat,
                cause=cause,
                features=self.features,
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

    def create_magic_item(
        self,
        name: str,
        item: str,
        description: str,
        *,
        size: str = "normal",
        damage: str = "",
        cost: int = 0,
        currency: str = "silver",
    ) -> dict[str, Any]:
        """Create a permanent magic item: charge its cost and give the character an item whose
        text records what it does. The GM calls it after the magic-item project finishes."""
        if not item.strip() or not description.strip():
            raise InvalidInput("a magic item needs a name and a description")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise InvalidInput("a magic item's cost must be a whole number, 0 or more")
        if damage and not is_formula(damage):
            raise InvalidInput(f"{damage!r} is not a dice formula such as 'd6'")
        rules = _required(self.inventory, "inventory")
        if size not in self.item_sizes:
            raise InvalidInput(f"there is no item size named {size!r}")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            if cost:
                if character.features.get(currency, 0) < cost:
                    raise InvalidInput(
                        f"creating {item} costs {cost} {currency}; "
                        f"{character.name} has {character.features.get(currency, 0)}"
                    )
                record(
                    db,
                    self.campaign_id,
                    CurrencyChanged(
                        name=character.name,
                        feature=currency,
                        amount=-cost,
                        cause=f"creating {item}",
                    ),
                )
            result = inventory_service.carry_item(
                db,
                self.campaign_id,
                rules,
                character.name,
                Item(item, size, self.item_sizes[size], text=description, damage=damage),
                cause=f"a magic item created by {character.name}",
            )
            db.commit()
            return {**result, "magic_item": item, "cost": cost, "currency": currency}

    def choose_feature_choice(
        self, name: str, feature: str, pick: str, choice: str
    ) -> dict[str, Any]:
        """Record a structured choice for a feature the character has, such as the attribute
        for Developed Attribute. The choice is validated against the feature's pick."""
        spec = self.character
        found = next(
            (
                item
                for item in spec.features.values()
                if item.id == feature or name_key(item.name) == name_key(feature)
            ),
            None,
        )
        if found is None:
            raise InvalidInput(f"there is no feature named {feature!r}")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            if character.features.get(found.id, 0) <= 0:
                raise InvalidInput(f"{character.name} does not have {found.name}")
            rule = next((item for item in found.picks if item.id == pick), None)
            if rule is None:
                raise InvalidInput(f"{found.name} has no choice named {pick!r}")
            choice = choice.strip()
            if not choice:
                raise InvalidInput("a feature choice cannot be empty")
            if rule.kind == "stat" and choice not in spec.stats:
                raise InvalidInput(f"{choice!r} is not an attribute")
            if rule.kind == "skill" and choice not in spec.skills:
                raise InvalidInput(f"{choice!r} is not a skill")
            if rule.choices and choice not in rule.choices:
                raise InvalidInput(f"{choice!r} is not one of {', '.join(rule.choices)}")
            if choice in rule.exclude:
                raise InvalidInput(f"{choice!r} cannot be chosen for {found.name}")
            key = f"{found.id}:{pick}"
            taken = character.feature_choices.get(key, [])
            allowed = rule.count * character.features.get(found.id, 0)
            if len(taken) >= allowed:
                raise InvalidInput(f"{found.name} has no {rule.label} choice left")
            if rule.kind != "text" and choice in taken:
                raise InvalidInput(f"{choice!r} is already chosen for {found.name}")
            skill = choice if rule.grant == "trained" else ""
            stat = ""
            if rule.grant == "modifier":
                bonus = spec.modifier(character.stats, choice) + character.stat_modifiers.get(
                    choice, 0
                )
                if rule.max_value and bonus >= rule.max_value:
                    raise InvalidInput(
                        f"{character.name}'s {spec.stats[choice].name} modifier is already at "
                        f"{rule.max_value}; {found.name} cannot raise it further"
                    )
                stat = choice
            record(
                db,
                self.campaign_id,
                FeatureChoiceMade(
                    name=character.name,
                    feature=found.id,
                    pick=pick,
                    choice=choice,
                    skill=skill,
                    stat=stat,
                ),
            )
            db.commit()
            return {
                "character": character.name,
                "feature": found.id,
                "pick": pick,
                "choice": choice,
            }

    def prepare_execution(self, name: str, weapon: str) -> dict[str, Any]:
        """Prepare an execution attack with a carried weapon. The target must be unaware or
        helpless when the attack is made."""
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            feature = self._weapon_feature(weapon)
            held = next(
                (item for item in character.items if same_item_name(item.name, weapon)), None
            )
            if feature is None and (held is None or not held.damage):
                raise InvalidInput(f"{weapon!r} is not a carried weapon with damage dice")
            record(
                db,
                self.campaign_id,
                ExecutionPrepared(name=character.name, weapon=weapon),
            )
            db.commit()
            return {"character": character.name, "weapon": weapon, "prepared": True}

    def execute(self, target: str, weapon: str) -> dict[str, Any]:
        """Make a prepared execution attack against an unaware or helpless target. A melee
        execution hits automatically and does the weapon's maximum damage."""
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            pc = state.pc()
            if pc is None:
                raise InvalidInput("there is no player character in this campaign")
            if not pc.execution_prepared or not same_item_name(pc.execution_weapon, weapon):
                raise InvalidInput(f"{pc.name} has not prepared an execution with {weapon!r}")
            struck = state.characters.get(name_key(target))
            if struck is None:
                raise InvalidInput(f"there is no character named {target!r}")
            helpless = {"unaware", "unconscious"}
            if not helpless.intersection(struck.conditions):
                raise InvalidInput(f"{struck.name} is aware and cannot be executed")
            feature = self._weapon_feature(weapon)
            held = next((item for item in pc.items if same_item_name(item.name, weapon)), None)
            dice = (
                feature.damage
                if feature is not None and feature.damage
                else (held.damage if held is not None else "")
            )
            if not dice:
                raise InvalidInput(f"{weapon!r} has no damage dice")
            damage = max_total(dice, self.plus)
            record(db, self.campaign_id, ExecutionTaken(name=pc.name))
            result = combat.player_attack(
                db,
                self.campaign_id,
                struck.name,
                weapon,
                self._rules(),
                self.rng,
                plus=self.plus,
                spec=self.character,
                weapon_feature=feature,
                attack_roll=20,
                damage_roll=damage,
                conditions=self.conditions,
                cell_feet=self._cell_feet(),
            )
            db.commit()
            return {**result, "execution": True, "damage": damage}

    def grapple(self, holder: str, target: str) -> dict[str, Any]:
        """Record a successful grapple. Both characters carry the grappled condition."""
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            attacker = state.characters.get(name_key(holder))
            struck = state.characters.get(name_key(target))
            if attacker is None:
                raise InvalidInput(f"there is no character named {holder!r}")
            if struck is None:
                raise InvalidInput(f"there is no character named {target!r}")
            if attacker.name == struck.name:
                raise InvalidInput("a character cannot grapple themselves")
            record(db, self.campaign_id, GrappleStarted(holder=attacker.name, target=struck.name))
            db.commit()
            return {"holder": attacker.name, "target": struck.name}

    def break_grapple(self, name: str) -> dict[str, Any]:
        """End a grapple, freeing both characters."""
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            if not character.grappling and not character.grappled_by:
                raise InvalidInput(f"{character.name} is not grappling")
            record(db, self.campaign_id, GrappleEnded(name=character.name))
            db.commit()
            return {"character": character.name}

    def surprise(self, name: str) -> dict[str, Any]:
        """Mark a character as surprised; they lose the configured number of turns."""
        rules = self._rules()
        if rules.surprise_turns <= 0:
            raise InvalidInput("this rule set has no surprise rule")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            record(db, self.campaign_id, Surprised(name=character.name, turns=rules.surprise_turns))
            db.commit()
            return {"character": character.name, "turns": rules.surprise_turns}

    def total_defense(self, name: str) -> dict[str, Any]:
        """Spend a Main action on total defense. The configured bonus lasts until the
        character's next turn."""
        rules = self._rules()
        if rules.total_defense_bonus <= 0:
            raise InvalidInput("this rule set has no total defense action")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            character = state.characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            if state.encounter is None:
                raise InvalidInput("total defense is only used in an encounter")
            record(
                db,
                self.campaign_id,
                TotalDefenseTaken(name=character.name, bonus=rules.total_defense_bonus),
            )
            db.commit()
            return {
                "character": character.name,
                "bonus": rules.total_defense_bonus,
                "armor": load_state(db, self.campaign_id)
                .characters[name_key(character.name)]
                .armor,
            }

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
                condition_ends=self.condition_ends,
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

    def place_participant(
        self, name: str, column: int, row: int, force: bool = False
    ) -> dict[str, Any]:
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
                force=force,
            )

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return add_note(db, self.campaign_id, text, subject)

    def record_setting(self, topic: str, detail: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return record_setting(db, self.campaign_id, topic, detail)

    def set_campaign_setting(self, setting: str, value: bool) -> dict[str, Any]:
        """Change a campaign-wide setting, only at the owner's explicit request."""
        # Campaigns imports this module, so the service is imported when the tool runs.
        from cygnusloom.services import campaigns as campaigns_service

        return campaigns_service.set_campaign_setting_from_turn(
            self.engine, self.campaign_id, setting, value
        )

    def set_music(self, mood: str) -> dict[str, Any]:
        """Set the background-music mood for the current scene. The browser chooses a track
        from the licensed pack; the GM never needs the catalog."""
        if mood not in MOODS:
            raise InvalidInput(f"mood must be one of: {', '.join(MOODS)}")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            scene = scene_key(state)
            cue = state.music
            if cue is not None and cue.mood == mood and cue.scene == scene:
                # The same cue in the same scene is already in the log. This keeps a model that
                # repeats the call from filling the transcript with identical events.
                return {"mood": mood}
            record(
                db,
                self.campaign_id,
                MusicCued(
                    mood=mood,
                    source="gm",
                    scene=scene,
                ),
            )
            db.commit()
        return {"mood": mood}

    def record_settings(self, facts: list[dict[str, str]]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return record_settings(db, self.campaign_id, facts)

    def update_note(self, note_id: int, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_note(db, self.campaign_id, note_id, text)

    def delete_note(self, note_id: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_note(db, self.campaign_id, note_id)

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_notes(load_state(db, self.campaign_id), query)

    def create_storyline(
        self,
        name: str,
        summary: str,
        plot: str | None = None,
        characters: Sequence[str] | None = None,
        locations: Sequence[str] | None = None,
        objects: Sequence[str] | None = None,
        notes: str | None = None,
        focus: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_storyline(
                db,
                self.campaign_id,
                name,
                summary,
                plot,
                characters,
                locations,
                objects,
                notes,
                focus,
            )

    def update_storyline(
        self,
        storyline: str,
        name: str | None = None,
        summary: str | None = None,
        plot: str | None = None,
        characters: Sequence[str] | None = None,
        locations: Sequence[str] | None = None,
        objects: Sequence[str] | None = None,
        notes: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_storyline(
                db,
                self.campaign_id,
                storyline,
                name,
                summary,
                plot,
                characters,
                locations,
                objects,
                notes,
                active,
            )

    def focus_storyline(self, storyline: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return focus_storyline(db, self.campaign_id, storyline)

    def deactivate_storyline(self, storyline: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return deactivate_storyline(db, self.campaign_id, storyline)

    def resolve_storyline(self, storyline: str, resolution: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return resolve_storyline(db, self.campaign_id, storyline, resolution)

    def search_storylines(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_storylines(load_state(db, self.campaign_id), query)

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

    def undo_narration(self, reason: str) -> dict[str, Any]:
        """Set the narration currently being reviewed aside. The narration was never recorded,
        so this records only the review's reason for the narration that replaces it."""
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            if state.turn is None or not state.turn.narrating:
                raise InvalidInput("there is no narration to review")
            note = bounded_text(reason, "reason", MAX_TEXT).strip()
            if not note:
                raise InvalidInput("reason must say what is wrong with the narration")
            record(db, self.campaign_id, NarrationUndone(reason=note))
            db.commit()
        return {"undone": "narration", "reason": note}

    def start_project(
        self,
        name: str,
        kind: str,
        goal: str,
        target: int = 0,
        force: bool = False,
        owner: str = "",
    ) -> dict[str, Any]:
        """Begin a project, research effort, magic working, or faction turn. A source-defined
        kind supplies its target, cost, currency, and required skill."""
        name = name.strip()
        kind = kind.strip()
        goal = goal.strip()
        if not name or not kind or not goal:
            raise InvalidInput("a project needs a name, a kind, and a goal")
        definition = self.projects.get(kind)
        if definition is not None:
            target = target or definition.target
        if isinstance(target, bool) or not isinstance(target, int) or target < 1:
            raise InvalidInput("a project's target must be a whole number, 1 or more")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            existing = state.projects.get(name_key(name))
            if existing is not None and not force:
                raise InvalidInput(f"there is already a project named {name!r}")
            payer = state.characters.get(name_key(owner)) if owner else state.pc()
            if definition is not None and definition.cost:
                if payer is None:
                    raise InvalidInput(f"{definition.label} needs a character to pay its cost")
                if (
                    definition.currency
                    and payer.features.get(definition.currency, 0) < definition.cost
                ):
                    raise InvalidInput(
                        f"{definition.label} costs {definition.cost} {definition.currency}; "
                        f"{payer.name} has {payer.features.get(definition.currency, 0)}"
                    )
                if (
                    definition.skill
                    and payer.skills.get(definition.skill, 0) < definition.skill_level
                ):
                    raise InvalidInput(
                        f"{definition.label} needs {definition.skill} "
                        f"level {definition.skill_level}"
                    )
                if definition.currency:
                    record(
                        db,
                        self.campaign_id,
                        CurrencyChanged(
                            name=payer.name,
                            feature=definition.currency,
                            amount=-definition.cost,
                            cause=f"starting {name}",
                        ),
                    )
            record(
                db,
                self.campaign_id,
                ProjectStarted(
                    name=name,
                    project_kind=kind,
                    goal=goal,
                    target=target,
                    cost=definition.cost if definition else 0,
                    currency=definition.currency if definition else "",
                    owner=payer.name if payer else "",
                ),
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
            "cost": project.cost,
            "currency": project.currency,
            "owner": project.owner,
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
        try:
            ident = resolve_id(
                self.character.features,
                wanted,
                kind="feature",
                names=lambda found: (found.name,),
            )
        except AmbiguousId as exc:
            raise InvalidInput(str(exc)) from exc
        except LookupError:
            ident = ""
        found = self.character.features.get(ident) if ident else None
        if found is None:
            with session_scope(self.engine) as db:
                state = load_state(db, self.campaign_id)
            located = custom_features.find_custom_feature_in_state(state, wanted)
            if located is not None:
                return custom_features.feature_view(located[1])
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

    def create_custom_feature(
        self,
        character: str,
        name: str,
        feature_type: str,
        description: str,
        tag: str = "",
        rank: int = 0,
        subject: str = "",
        base: str = "",
        mods: list[str] | None = None,
        stress: int = 0,
        cause: str = "",
    ) -> dict[str, Any]:
        """Create and learn a feature invented during play."""
        with session_scope(self.engine) as db:
            return custom_features.create_custom_feature(
                db,
                self.campaign_id,
                character,
                name,
                feature_type,
                description,
                tag=tag,
                rank=rank,
                subject=subject,
                base=base,
                mods=mods,
                stress=stress,
                cause=cause,
                feature_types=self.character.feature_types,
            )

    def update_custom_feature(
        self,
        character: str,
        feature: str,
        name: str | None = None,
        description: str | None = None,
        tag: str | None = None,
        rank: int | None = None,
        subject: str | None = None,
        base: str | None = None,
        mods: list[str] | None = None,
        stress: int | None = None,
    ) -> dict[str, Any]:
        """Change one of a character's invented features."""
        with session_scope(self.engine) as db:
            return custom_features.update_custom_feature(
                db,
                self.campaign_id,
                character,
                feature,
                name=name,
                description=description,
                tag=tag,
                rank=rank,
                subject=subject,
                base=base,
                mods=mods,
                stress=stress,
            )

    def remove_custom_feature(self, character: str, feature: str) -> dict[str, Any]:
        """Give up one of a character's invented features."""
        with session_scope(self.engine) as db:
            return custom_features.remove_custom_feature(db, self.campaign_id, character, feature)

    def list_custom_features(self, character: str) -> list[dict[str, Any]]:
        """List a character's invented features."""
        with session_scope(self.engine) as db:
            return custom_features.list_custom_features(db, self.campaign_id, character)

    def lookup_index(self) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return lookup_index(load_state(db, self.campaign_id))
