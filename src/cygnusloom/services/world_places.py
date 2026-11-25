"""Places: regions, locations, encounter maps, and the queries that read them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.content.icons import icon_registry
from cygnusloom.content.library import MAP_AMBIENTS, MAP_PALETTES, TERRAIN_PATTERNS
from cygnusloom.services import encounters
from cygnusloom.services import sites as sites_service
from cygnusloom.services.common import (
    InvalidInput,
    session_scope,
)
from cygnusloom.services.events import (
    load_state,
)
from cygnusloom.services.peoples import place_majority
from cygnusloom.services.state import WORLD_MAP_PALETTES
from cygnusloom.services.world_support import *


class PlacesMixin:
    def create_region(
        self,
        name: str,
        terrain: str,
        points: Sequence[Sequence[float]],
        force: bool = False,
        discovered: bool = False,
        color: str | None = None,
        pattern: str | None = None,
        ink: str | None = None,
        label: str | None = None,
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
                color=color,
                pattern=pattern,
                ink=ink,
                label=label,
            )

    def update_region(
        self,
        name: str,
        terrain: str | None = None,
        points: Sequence[Sequence[float]] | None = None,
        discovered: bool | None = None,
        color: str | None = None,
        pattern: str | None = None,
        ink: str | None = None,
        label: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_region(
                db,
                self.campaign_id,
                self.world_terrain,
                name,
                terrain=terrain,
                points=points,
                discovered=discovered,
                color=color,
                pattern=pattern,
                ink=ink,
                label=label,
                force=force,
            )

    def update_world_map(
        self,
        palette: str | None = None,
        ambient: str | None = None,
        background_color: str | None = None,
        line_color: str | None = None,
        show_clouds: bool | None = None,
        show_contours: bool | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_world_map(
                db,
                self.campaign_id,
                palette=palette,
                ambient=ambient,
                background_color=background_color,
                line_color=line_color,
                show_clouds=show_clouds,
                show_contours=show_contours,
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
        icon: str | None = None,
        summary: str | None = None,
        majority_species: str | None = None,
        site_type: str | None = None,
    ) -> dict[str, Any]:
        majority = (
            None if majority_species is None else place_majority(self.species, majority_species)
        )
        chosen_site = None
        if site_type is not None and site_type.strip():
            found = sites_service.resolve_site_type(self.site_types, site_type)
            if found is None:
                raise InvalidInput(
                    f"there is no site type named {site_type!r}; the site types are "
                    f"{', '.join(sorted(self.site_types)) or 'none'}"
                )
            chosen_site = found.id
        with session_scope(self.engine) as db:
            return create_location(
                db,
                self.campaign_id,
                name,
                description,
                discovered,
                x,
                y,
                force,
                parent,
                icon,
                summary,
                majority_species=majority,
                site_type=chosen_site,
            )

    def list_site_types(
        self, find: str = "", category: str = "", tier: int | None = None
    ) -> dict[str, Any]:
        """The kinds of place this rule set can generate, with their zones and find level."""
        find_text, category_text, tier_value = sites_service.filters(find, category, tier)
        cards = [
            sites_service.site_type_card(site_type)
            for site_type in sorted(
                self.site_types.values(), key=lambda item: (item.tier, item.name)
            )
            if (not find_text or site_type.find == find_text)
            and (not category_text or site_type.category == category_text)
            and (tier_value is None or site_type.tier == tier_value)
        ]
        result: dict[str, Any] = {"site_types": cards, "count": len(cards)}
        if not cards:
            result["note"] = "No site type matches those filters; relax one of them."
        return result

    def lookup_site_type(self, site_type: str) -> dict[str, Any]:
        """A site type's seed description, find hint, zones, loot, and threats."""
        found = sites_service.resolve_site_type(self.site_types, site_type)
        if found is None:
            names = ", ".join(sorted(self.site_types)) or "none"
            raise InvalidInput(
                f"there is no site type named {site_type!r}; the site types are {names}"
            )
        return sites_service.site_type_view(found, self._listed, self.threats)

    def search_site(
        self,
        site: str,
        character: str,
        zone: str = "",
        total: int = 8,
        take: bool = True,
        condition: str = "",
    ) -> dict[str, Any]:
        """Search one zone of a typed site, carrying the loot its profile gives."""
        with session_scope(self.engine) as db:
            result = sites_service.search_site(
                db,
                self.campaign_id,
                self.site_types,
                character,
                site,
                zone,
                total,
                self.rng,
                listed=self._listed,
                inventory=self.inventory,
                item_sizes=self.item_sizes,
                take=take,
                condition=condition,
            )
        if result["zone_noise"]:
            result["noise"] = self.advance_noise(
                result["zone_noise"], f"searching {result['zone_name']} at {result['site']}"
            )
        if "site_danger" in self.campaign_clocks:
            result["clock"] = self.advance_clock(
                "site_danger",
                set_to=result["danger"],
                reason=f"searching {result['zone_name']} at {result['site']}",
            )
        return result

    def discover_location(self, name: str, summary: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return discover_location(db, self.campaign_id, name, summary)

    def update_location(
        self,
        name: str,
        description: str | None = None,
        x: float | None = None,
        y: float | None = None,
        off_map: bool = False,
        force: bool = False,
        icon: str | None = None,
        summary: str | None = None,
        majority_species: str | None = None,
    ) -> dict[str, Any]:
        majority = (
            None if majority_species is None else place_majority(self.species, majority_species)
        )
        with session_scope(self.engine) as db:
            return update_location(
                db,
                self.campaign_id,
                name,
                description,
                x,
                y,
                off_map,
                force,
                icon,
                summary,
                majority_species=majority,
            )

    def describe_world(self, description: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return describe_world(db, self.campaign_id, description)

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
        palette: str = "",
        ambient: str = "",
        background_color: str = "",
        grid_color: str = "",
    ) -> dict[str, Any]:
        """Make a map at a location. The map is not necessarily an encounter: outside one it is a
        local map that can hold the player character, NPCs, and props. A template may supply the
        size and look, so `columns` and `rows` are optional, and every tool that makes a map
        requires its description."""
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
                palette=palette,
                ambient=ambient,
                background_color=background_color,
                grid_color=grid_color,
            )

    def create_local_map(
        self,
        name: str,
        location: str,
        description: str,
        template: str,
        columns: int | None = None,
        rows: int | None = None,
        palette: str = "",
        ambient: str = "",
        background_color: str = "",
        grid_color: str = "",
        regions: Sequence[Mapping[str, Any]] | None = None,
        tokens: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Make a local map at a location from a layout template. It is used outside an encounter
        to show characters and props where they stand, and becomes the encounter map when an
        encounter starts on it. The template's palette and ambient are copied unless given.

        `regions` paints rectangles of terrain and `tokens` places the cast, so a scene that needs
        both is one call. A character in `tokens` must already be at the map's location."""
        if regions is not None and (
            isinstance(regions, str) or not isinstance(regions, Sequence)
        ):
            raise InvalidInput("regions must be a list of rectangles")
        if tokens is not None and (
            isinstance(tokens, str) or not isinstance(tokens, Sequence)
        ):
            raise InvalidInput("tokens must be a list of placements")
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
                palette=palette,
                ambient=ambient,
                background_color=background_color,
                grid_color=grid_color,
            )
            cells_changed = 0
            for region in regions or []:
                if not isinstance(region, Mapping):
                    raise InvalidInput("each region must be an object with two corners")
                missing = [
                    key
                    for key in ("from_column", "from_row", "to_column", "to_row")
                    if key not in region
                ]
                if missing:
                    raise InvalidInput(
                        "each region needs from_column, from_row, to_column, and to_row"
                    )
                filled = encounters.fill_map_cells(
                    db,
                    self.campaign_id,
                    name,
                    region["from_column"],
                    region["from_row"],
                    region["to_column"],
                    region["to_row"],
                    self.terrain,
                    self.encounter_objects,
                    force=bool(region.get("force", False)),
                    terrain_id=region.get("terrain"),
                    object_id=region.get("object"),
                    color=region.get("color"),
                    pattern=region.get("pattern"),
                    icon=region.get("icon"),
                    label=region.get("label"),
                )
                cells_changed += filled["changed"]
            tokens_placed = 0
            for token in tokens or []:
                if not isinstance(token, Mapping):
                    raise InvalidInput("each token must be an object with a name and cell")
                missing = [key for key in ("name", "column", "row") if key not in token]
                if missing:
                    raise InvalidInput("each token needs a name, column, and row")
                encounters.place_map_token(
                    db,
                    self.campaign_id,
                    name,
                    token["name"],
                    token["column"],
                    token["row"],
                    self.encounter_objects,
                )
                tokens_placed += 1
        result["created"] = "local map"
        if regions is not None:
            result["cells_changed"] = cells_changed
        if tokens is not None:
            result["tokens_placed"] = tokens_placed
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

    def set_map_cells(
        self, map: str, cells: list[Mapping[str, Any]], force: bool = False
    ) -> dict[str, Any]:
        """Set terrain or objects on map cells, inside or outside an encounter."""
        return self.set_encounter_map_cells(map, cells, force=force)

    def fill_map_cells(
        self,
        map: str,
        from_column: int,
        from_row: int,
        to_column: int,
        to_row: int,
        terrain: str | None = None,
        object: str | None = None,
        color: str | None = None,
        pattern: str | None = None,
        icon: str | None = None,
        label: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fill a rectangle of map cells with one terrain, object, colour, pattern, icon, or
        label. The corners may be given in either order and are both included. Use it for a
        whole floor or other broad area instead of listing every cell with set_map_cells."""
        with session_scope(self.engine) as db:
            return encounters.fill_map_cells(
                db,
                self.campaign_id,
                map,
                from_column,
                from_row,
                to_column,
                to_row,
                self.terrain,
                self.encounter_objects,
                force=force,
                terrain_id=terrain,
                object_id=object,
                color=color,
                pattern=pattern,
                icon=icon,
                label=label,
            )

    def set_encounter_map_cells(
        self, map: str, cells: list[Mapping[str, Any]], force: bool = False
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.set_map_cells(
                db,
                self.campaign_id,
                map,
                cells,
                self.terrain,
                self.encounter_objects,
                force=force,
            )

    def update_encounter_map(
        self,
        name: str,
        columns: int | None = None,
        rows: int | None = None,
        description: str | None = None,
        new_name: str | None = None,
        location: str | None = None,
        palette: str | None = None,
        ambient: str | None = None,
        background: str | None = None,
        background_color: str | None = None,
        grid_color: str | None = None,
        extend_left: int = 0,
        extend_right: int = 0,
        extend_up: int = 0,
        extend_down: int = 0,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.update_map(
                db,
                self.campaign_id,
                name,
                columns,
                rows,
                description,
                new_name,
                location,
                palette=palette,
                ambient=ambient,
                background=background,
                background_color=background_color,
                grid_color=grid_color,
                extend_left=extend_left,
                extend_right=extend_right,
                extend_up=extend_up,
                extend_down=extend_down,
                terrain=self.terrain,
            )

    def update_local_map(
        self,
        map: str,
        new_name: str | None = None,
        description: str | None = None,
        location: str | None = None,
        columns: int | None = None,
        rows: int | None = None,
        extend_left: int = 0,
        extend_right: int = 0,
        extend_up: int = 0,
        extend_down: int = 0,
        background: str | None = None,
        background_color: str | None = None,
        grid_color: str | None = None,
        palette: str | None = None,
        ambient: str | None = None,
    ) -> dict[str, Any]:
        """Rename, resize, restyle, or extend a local map. Extending left or up shifts every
        token and mark, so the scene is kept."""
        with session_scope(self.engine) as db:
            result = encounters.update_map(
                db,
                self.campaign_id,
                map,
                columns,
                rows,
                description,
                new_name,
                location,
                palette=palette,
                ambient=ambient,
                background=background,
                background_color=background_color,
                grid_color=grid_color,
                extend_left=extend_left,
                extend_right=extend_right,
                extend_up=extend_up,
                extend_down=extend_down,
                terrain=self.terrain,
            )
        result["updated"] = "local map"
        return result

    def map_options(self) -> dict[str, Any]:
        """The terrains, objects, templates, palettes, ambients, patterns, and world map looks
        a map may use."""
        place_icons = [
            {"id": icon_id, "label": entry.get("label", icon_id)}
            for icon_id, entry in sorted(icon_registry().items())
            if entry.get("category") == "places"
        ]
        return {
            "world": {
                "terrains": [
                    {
                        "id": terrain.id,
                        "name": terrain.name,
                        "color": terrain.color,
                        "pattern": terrain.pattern or "",
                        "ink": terrain.ink or "",
                        "rule": terrain.rule,
                    }
                    for terrain in sorted(self.world_terrain.values(), key=lambda entry: entry.id)
                ],
                "palettes": list(WORLD_MAP_PALETTES),
                "ambients": list(MAP_AMBIENTS),
                "place_icons": place_icons,
            },
            "terrains": [
                {
                    "id": terrain.id,
                    "name": terrain.name,
                    "color": terrain.color,
                    "pattern": terrain.pattern or "",
                    "difficult": terrain.difficult,
                    "category": terrain.category,
                }
                for terrain in sorted(self.terrain.values(), key=lambda entry: entry.id)
            ],
            "objects": [
                {
                    "id": item.id,
                    "name": item.name,
                    "category": item.category,
                    "blocks": item.blocks,
                    "solid": item.solid,
                    "icon": item.icon,
                    "description": item.description,
                }
                for item in sorted(self.encounter_objects.values(), key=lambda entry: entry.id)
            ],
            "templates": [
                {
                    "id": template.id,
                    "name": template.name,
                    "columns": template.columns,
                    "rows": template.rows,
                    "palette": template.palette,
                    "ambient": template.ambient,
                    "description": template.description,
                }
                for template in sorted(
                    self.encounter_templates.values(), key=lambda entry: entry.id
                )
            ],
            "palettes": list(MAP_PALETTES),
            "ambients": list(MAP_AMBIENTS),
            "patterns": list(TERRAIN_PATTERNS),
        }

    def delete_encounter_map(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.delete_map(db, self.campaign_id, name)
