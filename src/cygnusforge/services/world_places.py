"""Places: regions, locations, encounter maps, and the queries that read them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusforge.services import encounters
from cygnusforge.services.common import (
    session_scope,
)
from cygnusforge.services.events import (
    load_state,
)
from cygnusforge.services.world_support import *


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
