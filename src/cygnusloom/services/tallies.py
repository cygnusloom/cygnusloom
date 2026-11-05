"""Running campaign scores, such as each House's points. The rule set's JSON lists the tallies and their
entries, so this module only checks the request and records the change."""

from __future__ import annotations

from sqlmodel import Session

from cygnusloom.content.library import TallyRules
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import TallyChanged, load_state, record


def adjust_tally(
    db: Session,
    campaign_id: int,
    tally: TallyRules | None,
    entry: str,
    change: int,
    *,
    entry_label: str = "",
    reason: str = "",
    award: str = "",
    force: bool = False,
) -> dict[str, object]:
    """Add `change` to one entry of a tally. A negative change deducts from it. `award` is a key for
    the event being paid out; a tally refuses an award key it has already paid, so one event is not
    counted twice. A gain equal to one the entry already paid this period is refused as a likely
    repeat, unless `force` is set. A dynamic tally accepts an entry the campaign made, with
    `entry_label` as its display name."""
    if tally is None:
        raise InvalidInput("this rule set keeps no tallies")
    label = tally.entries.get(entry)
    if label is None:
        if not tally.dynamic:
            raise InvalidInput(
                f"there is no {tally.label} entry {entry!r}; choose one of {sorted(tally.entries)}"
            )
        entry = entry.strip()
        if not entry:
            raise InvalidInput(f"{tally.label} needs an entry id")
        label = entry_label.strip() or entry
    state = load_state(db, campaign_id)
    key = award.strip()
    if key and key in state.tally_awards.get(tally.id, set()):
        raise InvalidInput(
            f"{tally.label} has already paid the award {key!r}; do not award it twice"
        )
    if change > 0 and not force and change in state.tally_gains.get(tally.id, {}).get(entry, set()):
        raise InvalidInput(
            f"{label} already gained {change} {tally.label} this period. If this is a new award, "
            "call again with force=true; otherwise do not award it again."
        )
    before = state.tallies.get(tally.id, {}).get(entry, 0)
    after = before + change
    record(
        db,
        campaign_id,
        TallyChanged(
            tally=tally.id,
            label=tally.label,
            entry=entry,
            entry_label=label,
            before=before,
            after=after,
            cause=reason,
            award=key,
        ),
    )
    db.commit()
    return {"tally": tally.label, "entry": label, "before": before, "value": after}
