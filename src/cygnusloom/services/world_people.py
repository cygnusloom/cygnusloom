"""People: NPCs, hirelings, monsters, and the queries that read them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.content.character import (
    choice_options,
)
from cygnusloom.services.common import (
    session_scope,
)
from cygnusloom.services.events import (
    load_state,
)
from cygnusloom.services.inventory import tracks_inventory
from cygnusloom.services.views import (
    get_statblock,
    speaker_profile,
)
from cygnusloom.services.world_support import *


class PeopleMixin:
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

    def promote_hireling(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return promote_hireling(db, self.campaign_id, name)

    def dismiss_hireling(self, name: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return dismiss_hireling(db, self.campaign_id, name, cause)

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

    def describe_character(self, name: str, summary: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return describe_character(db, self.campaign_id, name, summary)

    def delete_npc(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_npc(db, self.campaign_id, name)

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
                track_inventory=tracks_inventory(db, self.campaign_id),
            )

    def character_options(self, field: str | None = None) -> dict[str, Any]:
        return choice_options(self.character, field)

    def speaker_profile(self, name: str) -> str:
        with session_scope(self.engine) as db:
            return speaker_profile(load_state(db, self.campaign_id), name, self.character)

    def speaker_at_player(self, name: str) -> bool:
        with session_scope(self.engine) as db:
            return speaker_at_player(load_state(db, self.campaign_id), name)
