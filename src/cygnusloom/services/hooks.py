"""Calls into a rule set's hook functions with a context the engine provides.

The engine only knows the Hooks object. The functions it calls are named by the rule set's
hooks definition and live in the rule set's Python module."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from cygnusloom.content.library import Hooks, InventoryRules, Marketplace
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    CampaignState,
    Character,
    MarkSet,
    load_state,
    name_key,
    record,
)


@dataclass
class HookContext:
    """What a hook gets. `db` and `campaign_id` are None when a hook only renders, such as the
    status hook, and then the hook must read what it needs from the character it is given."""

    db: Session | None
    campaign_id: int | None
    hooks: Hooks | None
    inventory: InventoryRules | None
    # The rule set's structured data, so a hook such as WWN's after_rest can resolve its own
    # progression without holding a World service.
    rule_data: Mapping[str, object] = field(default_factory=dict)
    # The catalogue, so an upkeep hook can restore the uses of a legacy consumable that was
    # created before catalogue uses were tracked.
    marketplace: Marketplace | None = None
    # Who or what meets the party's food, water, and shelter for this rest, or "" when the
    # packs must supply the day.
    needs_met_by: str = ""
    # Whether this campaign tracks food, water, and rest privation. False means an upkeep hook
    # must spend no supplies and end the privation it would otherwise record.
    survival_needs: bool = True

    @classmethod
    def for_rendering(cls, hooks: Hooks | None, inventory: InventoryRules | None) -> HookContext:
        return cls(None, None, hooks, inventory)

    def session(self) -> tuple[Session, int]:
        if self.db is None or self.campaign_id is None:
            raise RuntimeError("this hook context is for rendering only")
        return self.db, self.campaign_id

    def state(self) -> CampaignState:
        db, campaign_id = self.session()
        return load_state(db, campaign_id)

    def set_mark(self, name: str, key: str, value: Any, text: str) -> None:
        """Set a character's mark, or clear it when value is None, and record the change."""
        db, campaign_id = self.session()
        record(db, campaign_id, MarkSet(name=name, key=key, value=value, text=text))

    def character(self, name: str) -> Character:
        """The named character, raising InvalidInput when the state has none."""
        character = self.state().characters.get(name_key(name))
        if character is None:
            raise InvalidInput(f"there is no character named {name!r}")
        return character

    def marks(self, name: str) -> dict[str, Any]:
        return self.character(name).marks


def call_hook(
    hooks: Hooks | None, hook_name: str, hc: HookContext, *args: Any, default: Any
) -> Any:
    """Call the named hook with the context first. Return default when the rule set does not
    declare that hook."""
    if hooks is None:
        return default
    function_name = hooks.function_name(hook_name)
    if function_name is None:
        return default
    return getattr(hooks.module, function_name)(hc, *args)
