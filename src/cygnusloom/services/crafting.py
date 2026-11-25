"""Crafting, salvage, and repair. A recipe names inputs, tools, a skill, a difficulty, and an
output; the GM rolls the check first and passes its total here. The engine consumes the inputs
and records the output or the failure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, ItemTemplate
from cygnusloom.content.library import RecipeDefinition
from cygnusloom.services.events import (
    EffectNoted,
    ItemAdded,
    ItemConditionChanged,
    ItemRemoved,
    ItemUsesSet,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import Item, alias_key


def _recipe(name: str, recipes: Mapping[str, RecipeDefinition]) -> RecipeDefinition:
    recipe = recipes.get(name)
    if recipe is not None:
        return recipe
    wanted = alias_key(name)
    for candidate in recipes.values():
        if alias_key(candidate.id) == wanted or alias_key(candidate.name) == wanted:
            return candidate
    raise ValueError(f"there is no recipe named {name!r}")


def _template(spec: CharacterSpec, key: str) -> ItemTemplate:
    template = spec.items.get(key)
    if template is not None:
        return template
    wanted = name_key(key)
    for candidate in spec.items.values():
        if name_key(candidate.name) == wanted:
            return candidate
    raise ValueError(f"there is no item named {key!r}")


def _held(character: Any, spec: CharacterSpec, key: str) -> Item | None:
    template = _template(spec, key)
    wanted = name_key(template.name)
    return next((item for item in character.items if name_key(item.name) == wanted), None)


def _has_tool(character: Any, spec: CharacterSpec, key: str) -> bool:
    return _held(character, spec, key) is not None


def _consume(
    db: Session,
    campaign_id: int,
    character_name: str,
    spec: CharacterSpec,
    key: str,
    uses: int,
    cause: str,
) -> None:
    character = load_state(db, campaign_id).characters[name_key(character_name)]
    held = _held(character, spec, key)
    if held is None:
        raise ValueError(f"{character.name} is not carrying {key!r}")
    if uses <= 0:
        return
    if held.max_uses or held.uses:
        left = max(0, held.uses - uses)
        if left <= 0:
            record(db, campaign_id, ItemRemoved(name=character.name, item=held.name))
        else:
            record(
                db,
                campaign_id,
                ItemUsesSet(name=character.name, item=held.name, uses=left, cause=cause),
            )
    else:
        record(db, campaign_id, ItemRemoved(name=character.name, item=held.name))


def _grant(
    db: Session,
    campaign_id: int,
    character_name: str,
    template: ItemTemplate,
    *,
    uses: int,
    condition: str,
    cause: str,
) -> None:
    record(
        db,
        campaign_id,
        ItemAdded(
            name=character_name,
            item=template.name,
            size=template.size,
            slots=1,
            kind_of_item=template.tag,
            cause=cause,
            text=template.description,
            uses=uses,
            max_uses=max(uses, template.uses),
            damage=template.damage,
            rules=list(template.rules),
            flags=list(template.flags),
            loaded=template.capacity,
            capacity=template.capacity,
            caliber=template.caliber,
            condition=condition,
        ),
    )


def craft(
    db: Session,
    campaign_id: int,
    character_name: str,
    recipe_name: str,
    total: int,
    *,
    spec: CharacterSpec,
    recipes: Mapping[str, RecipeDefinition],
) -> dict[str, Any]:
    recipe = _recipe(recipe_name, recipes)
    character = load_state(db, campaign_id).characters[name_key(character_name)]
    for tool in recipe.tools:
        if not _has_tool(character, spec, tool):
            raise ValueError(f"{character.name} needs {tool!r} to make {recipe.name}")
    for entry in recipe.inputs:
        if _held(character, spec, str(entry["item"])) is None:
            raise ValueError(f"{character.name} lacks {entry['item']!r} for {recipe.name}")
    success = total >= recipe.difficulty
    for entry in recipe.inputs:
        _consume(
            db,
            campaign_id,
            character.name,
            spec,
            str(entry["item"]),
            int(entry.get("uses", 1)),
            f"used in {recipe.name}",
        )
    if success:
        output = spec.items[str(recipe.output["item"])]
        _grant(
            db,
            campaign_id,
            character.name,
            output,
            uses=int(recipe.output.get("uses", output.uses)),
            condition=str(recipe.output.get("condition", "serviceable")),
            cause=f"made by {recipe.name}",
        )
        record(
            db,
            campaign_id,
            EffectNoted(name=character.name, text=f"{character.name} makes {output.name}."),
        )
    else:
        record(
            db,
            campaign_id,
            EffectNoted(
                name=character.name,
                text=f"{recipe.name} fails; the materials are spent ({recipe.risk or 'wasted'}).",
            ),
        )
    db.commit()
    return {
        "recipe": recipe.name,
        "character": character.name,
        "total": total,
        "difficulty": recipe.difficulty,
        "success": success,
        "output": recipe.output["item"] if success else "",
    }


def salvage(
    db: Session,
    campaign_id: int,
    character_name: str,
    *,
    total: int,
    difficulty: int,
    spec: CharacterSpec,
    parts_item: str = "parts",
    amount: int = 1,
) -> dict[str, Any]:
    character = load_state(db, campaign_id).characters[name_key(character_name)]
    if total < difficulty:
        record(
            db,
            campaign_id,
            EffectNoted(name=character.name, text=f"{character.name} finds no usable parts."),
        )
        db.commit()
        return {"character": character.name, "success": False, "parts": 0}
    template = _template(spec, parts_item)
    _grant(
        db,
        campaign_id,
        character.name,
        template,
        uses=amount,
        condition="serviceable",
        cause="salvaged",
    )
    db.commit()
    return {"character": character.name, "success": True, "parts": amount}


def repair_item(
    db: Session,
    campaign_id: int,
    character_name: str,
    item_name: str,
    *,
    total: int,
    difficulty: int,
    spec: CharacterSpec,
    condition: str = "",
) -> dict[str, Any]:
    character = load_state(db, campaign_id).characters[name_key(character_name)]
    held = _held(character, spec, item_name)
    if held is None:
        raise ValueError(f"{character.name} is not carrying {item_name!r}")
    if total < difficulty:
        record(
            db,
            campaign_id,
            EffectNoted(
                name=character.name,
                text=f"{character.name} cannot repair {held.name} yet.",
            ),
        )
        db.commit()
        return {"item": held.name, "success": False, "condition": held.condition}
    order = ["broken", "worn", "serviceable", "fine"]
    current = held.condition if held.condition in order else "serviceable"
    index = min(len(order) - 1, order.index(current) + 1)
    after = condition or order[index]
    record(
        db,
        campaign_id,
        ItemConditionChanged(
            name=character.name,
            item=held.name,
            condition=after,
            cause="repaired",
        ),
    )
    db.commit()
    return {"item": held.name, "success": True, "condition": after}
