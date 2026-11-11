"""Resolve the loose ids an AI game master writes to the exact ids a rule set or world defines.

The model often capitalizes an id or writes a display name, such as `Sneak` for `sneak` or
`Dexterity` for `dex`. Matching is case- and punctuation-insensitive, but never fuzzy: an exact
id always wins, and a name that fits more than one definition is an error rather than a guess.
Mutation tools must never silently pick one of several candidates."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

# How many candidates an ambiguity lists before it says how many more there are.
MAX_CANDIDATES = 8
# How many ids an unknown-id error lists as a reminder of the available choices.
MAX_AVAILABLE_IDS = 20


class AmbiguousId(LookupError):
    """A loose id or display name fits more than one definition, so no id can be chosen."""


def loose_key(text: str) -> str:
    """A comparison key for an id or display name: lowercase, with every non-alphanumeric
    character removed. `Sneak`, `sneak`, and `Sneak!` share one key."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _display_names(item: object) -> tuple[str, ...]:
    """The default display names of a table entry: its `name` attribute, when it has one."""
    name = getattr(item, "name", "")
    return (name,) if isinstance(name, str) else ()


def resolve_id[T](
    table: Mapping[str, T],
    value: str,
    *,
    kind: str,
    names: Callable[[T], Iterable[str]] | None = _display_names,
) -> str:
    """The id from `table` that `value` names.

    An exact key always wins. Then an id that matches after `loose_key` normalization, then a
    display name from `names`. Exactly one match is required; several raise `AmbiguousId` with
    the candidate ids, and none raises `LookupError` listing the available ids."""
    wanted = value.strip()
    if wanted in table:
        return wanted
    key = loose_key(wanted)
    if not key:
        raise LookupError(f"there is no {kind} named {value!r}; {_available(table, kind)}")
    id_matches = [ident for ident in table if loose_key(ident) == key]
    if len(id_matches) == 1:
        return id_matches[0]
    name_matches: list[str] = []
    if names is not None:
        for ident, item in table.items():
            for name in names(item):
                if isinstance(name, str) and loose_key(name) == key:
                    name_matches.append(ident)
                    break
    candidates = sorted({*id_matches, *name_matches})
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        shown = ", ".join(candidates[:MAX_CANDIDATES])
        if len(candidates) > MAX_CANDIDATES:
            shown = f"{shown} (and {len(candidates) - MAX_CANDIDATES} more)"
        raise AmbiguousId(
            f"{value!r} matches more than one {kind}: {shown}; pass the exact id instead"
        )
    raise LookupError(f"there is no {kind} named {value!r}; {_available(table, kind)}")


def resolve_value[T](
    table: Mapping[str, T],
    value: str,
    *,
    kind: str,
    names: Callable[[T], Iterable[str]] | None = _display_names,
) -> T:
    """The table entry that `value` names, using the same rules as `resolve_id`."""
    return table[resolve_id(table, value, kind=kind, names=names)]


def _available(table: Mapping[str, object], kind: str) -> str:
    """A bounded reminder of the ids a table defines."""
    ids = sorted(table)
    if not ids:
        return f"no {kind} ids are defined"
    if len(ids) <= MAX_AVAILABLE_IDS:
        return f"the {kind} ids are: {', '.join(ids)}"
    shown = ", ".join(ids[:MAX_AVAILABLE_IDS])
    return f"some {kind} ids are: {shown} (and {len(ids) - MAX_AVAILABLE_IDS} more)"
