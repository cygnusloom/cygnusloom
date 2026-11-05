"""Places: regions, locations, encounter maps, and the queries that read them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.services import encounters
from cygnusloom.services.common import (
    session_scope,
)
from cygnusloom.services.events import (
    load_state,
)
from cygnusloom.services.world_support import *


class PlacesMixin:
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

    def all_at_location(self, location: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return all_at_location(load_state(db, self.campaign_id), location)

    def search_location(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_location(load_state(db, self.campaign_id), query)

    def create_encounter_map(
        self,
        name: str,
        location: str,
        columns: int | None = None,
        rows: int | None = None,
        description: str = "",
        template: str = "",
    ) -> dict[str, Any]:
        """Make a map at a location. The map is not necessarily an encounter: outside one it is a
        local map that can hold the player character, NPCs, and props. A template may supply the
        size, so `columns` and `rows` are optional, and every tool that makes a map requires its
        description."""
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

    def create_local_map(
        self,
        name: str,
        location: str,
        description: str,
        template: str,
        columns: int | None = None,
        rows: int | None = None,
    ) -> dict[str, Any]:
        """Make a local map at a location from a layout template. It is used outside an encounter
        to show characters and props where they stand, and becomes the encounter map when an
        encounter starts on it."""
        with session_scope(self.engine) as db:
            result = encounters.create_map(
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
        result["created"] = "local map"
        return result

    def place_map_token(self, map: str, name: str, column: int, row: int) -> dict[str, Any]:
        """Place a character, NPC, or prop token on a local map outside an encounter."""
        with session_scope(self.engine) as db:
            return encounters.place_map_token(
                db,
                self.campaign_id,
                map,
                name,
                column,
                row,
                self.encounter_objects,
            )

    def remove_map_token(self, map: str, name: str) -> dict[str, Any]:
        """Remove a token from a local map outside an encounter."""
        with session_scope(self.engine) as db:
            return encounters.remove_map_token(db, self.campaign_id, map, name)

    def set_map_cells(self, map: str, cells: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Set terrain or objects on map cells, inside or outside an encounter."""
        return self.set_encounter_map_cells(map, cells)

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
