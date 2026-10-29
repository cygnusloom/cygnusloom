"""Calls into a rule set's hook functions with a context the engine provides.

The engine only knows the Hooks object. The functions it calls are named by the rule set's
hooks definition and live in the rule set's Python module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from cygnusforge.content.library import Hooks, InventoryRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import CampaignState, MarkSet, load_state, name_key, record


@dataclass
class HookContext:
    """What a hook gets. `db` and `campaign_id` are None when a hook only renders, such as the
    status hook, and then the hook must read what it needs from the character it is given."""

    db: Session | None
    campaign_id: int | None
    hooks: Hooks | None
    inventory: InventoryRules | None

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

    def marks(self, name: str) -> dict[str, Any]:
        characters = self.state().characters
        if name_key(name) not in characters:
            raise InvalidInput(f"there is no character named {name!r}")
        return characters[name_key(name)].marks


def call_hook(
    hooks: Hooks | None, hook_name: str, hc: HookContext, *args: Any, default: Any
) -> Any:
    """Call the named hook with the context first. Return default when the rule set has no such hook."""
    if hooks is None:
        return default
    function_name = getattr(hooks, hook_name)
    if function_name is None:
        return default
    return getattr(hooks.module, function_name)(hc, *args)
