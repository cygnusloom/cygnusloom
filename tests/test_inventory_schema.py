"""The inventory definition's named pools, size assignment, and strict validation.

Each test writes its own minimal content tree and asserts what the public `RuleSet` carries back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import ContentLibrary
from tests.support import DEFAULT_RULE_SET, content_tree


def _stat() -> dict[str, Any]:
    return {"type": "stat", "id": "resolve", "name": "Resolve", "derived": {"base": 12}}


def _item_sizes() -> list[dict[str, Any]]:
    return [
        {"type": "item_size", "id": "tiny", "slots": 0},
        {"type": "item_size", "id": "normal", "slots": 1},
        {"type": "item_size", "id": "bulky", "slots": 2},
    ]


def _inventory(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "type": "inventory",
        "id": "pack",
        "capacity": 10,
        "penalty_name": "Overloaded",
        "penalty_lasts": "until you rest",
        "penalty_item": "overload",
        "penalty_slots": 1,
    }
    definition.update(overrides)
    return definition


def _pool(ident: str, **overrides: Any) -> dict[str, Any]:
    pool: dict[str, Any] = {"id": ident, "name": ident.title(), "stat": "resolve"}
    pool.update(overrides)
    return pool


def _load(tmp_path: Path, inventory: dict[str, Any]):
    tree = content_tree(
        tmp_path,
        definitions=[_stat(), *_item_sizes(), inventory],
    )
    return ContentLibrary.load(tree).rule_sets[DEFAULT_RULE_SET]


def test_named_pools_load(tmp_path: Path):
    rule_set = _load(
        tmp_path,
        _inventory(
            pools=[
                _pool("pack", multiplier=1.5),
                _pool("hand", divisor=2, ready=True),
                {"id": "satchel", "name": "Satchel", "capacity": 4},
            ],
            size_pools={"bulky": "pack"},
            default_pool="pack",
        ),
    )

    rules = rule_set.inventory
    assert rules is not None
    assert [pool.id for pool in rules.pools] == ["pack", "hand", "satchel"]
    assert [pool.name for pool in rules.pools] == ["Pack", "Hand", "Satchel"]
    assert rules.size_pools == {"bulky": "pack"}
    assert rules.default_pool == "pack"
    pack, hand, satchel = rules.pools
    assert pack.ready is False
    assert hand.ready is True
    assert pack.capacity_for(13) == 19
    assert hand.capacity_for(13) == 6
    assert satchel.capacity_for(99) == 4


def test_pool_rounding_and_base(tmp_path: Path):
    rule_set = _load(
        tmp_path,
        _inventory(
            pools=[_pool("pouch", base=1, divisor=3, rounding="up")],
            default_pool="pouch",
        ),
    )

    rules = rule_set.inventory
    assert rules is not None
    pouch = rules.pools[0]
    assert pouch.rounding == "up"
    assert pouch.base == 1
    assert pouch.capacity_for(13) == 5


def test_duplicate_pool_id_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match="duplicate pool id 'pack'"):
        _load(
            tmp_path,
            _inventory(pools=[_pool("pack"), _pool("pack")], default_pool="pack"),
        )


def test_unknown_size_pool_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match=r"size pool 'giant': names no item size"):
        _load(
            tmp_path,
            _inventory(
                pools=[_pool("pack")],
                size_pools={"giant": "pack"},
                default_pool="pack",
            ),
        )


def test_size_pool_must_name_a_pool(tmp_path: Path):
    with pytest.raises(ContentError, match=r"size pool 'normal': names no pool"):
        _load(
            tmp_path,
            _inventory(
                pools=[_pool("pack")],
                size_pools={"normal": "nowhere"},
                default_pool="pack",
            ),
        )


def test_missing_default_pool_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match="needs a 'default_pool'"):
        _load(tmp_path, _inventory(pools=[_pool("pack")]))


def test_default_pool_must_name_a_pool(tmp_path: Path):
    with pytest.raises(ContentError, match="default_pool.*names no pool"):
        _load(tmp_path, _inventory(pools=[_pool("pack")], default_pool="nowhere"))


def test_unknown_pool_key_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match="unknown key"):
        _load(
            tmp_path,
            _inventory(
                pools=[_pool("pack", colour="red")],
                default_pool="pack",
            ),
        )


def test_at_most_one_pool_may_be_readied(tmp_path: Path):
    with pytest.raises(ContentError, match="at most one pool may be readied"):
        _load(
            tmp_path,
            _inventory(
                pools=[_pool("left", ready=True), _pool("right", ready=True)],
                default_pool="left",
            ),
        )


def test_pool_numeric_fields_are_validated(tmp_path: Path):
    with pytest.raises(ContentError, match="'divisor' must be a whole number, at least 1"):
        _load(
            tmp_path,
            _inventory(pools=[_pool("pack", divisor=0)], default_pool="pack"),
        )


def test_single_pool_ruleset_is_unchanged(tmp_path: Path):
    rule_set = _load(
        tmp_path,
        _inventory(
            capacity_stat="resolve",
            capacity_multiplier=1.5,
            readied_stat="resolve",
            readied_divisor=2,
        ),
    )

    rules = rule_set.inventory
    assert rules is not None
    assert rules.capacity == 10
    assert rules.capacity_stat == "resolve"
    assert rules.capacity_multiplier == 1.5
    assert rules.readied_stat == "resolve"
    assert rules.readied_divisor == 2
    assert rules.pools == ()
    assert rules.size_pools == {}
    assert rules.default_pool == ""
