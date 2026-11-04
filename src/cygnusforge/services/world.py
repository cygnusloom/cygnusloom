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

from cygnusforge.content.character import (
    CharacterSpec,
    Feature,
    TableOption,
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
    WorldTerrain,
)
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
    PC_KIND,
    MaxChanged,
    ProjectAbandoned,
    ProjectAdvanced,
    ProjectStarted,
    Recovered,
    SkillChanged,
    current,
    load_state,
    name_key,
    record,
    same_item_name,
)
from cygnusforge.services.hooks import HookContext, call_hook
from cygnusforge.services.recovery import recover
from cygnusforge.services.views import (
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


from cygnusforge.services.world_people import PeopleMixin
from cygnusforge.services.world_places import PlacesMixin
from cygnusforge.services.world_support import *
from cygnusforge.services.world_support import _initialize_generated_defense, _required
from cygnusforge.services.world_travel import TravelMixin


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
            encumbrance_checks=relic.encumbrance_checks,
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
                armor_penalty=self._armor_check_penalty(db, character, skill),
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



