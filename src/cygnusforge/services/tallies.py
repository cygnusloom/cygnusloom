"""Running campaign scores, such as each House's points. The rule set's JSON lists the tallies and their
entries, so this module only checks the request and records the change."""

from __future__ import annotations

from sqlmodel import Session

from cygnusforge.content.library import TallyRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import TallyChanged, load_state, record


def adjust_tally(
    db: Session,
    campaign_id: int,
    tally: TallyRules | None,
    entry: str,
    change: int,
    *,
    reason: str = "",
) -> dict[str, object]:
    """Add `change` to one entry of a tally. A negative change deducts from it."""
    if tally is None:
        raise InvalidInput("this rule set keeps no tallies")
    label = tally.entries.get(entry)
    if label is None:
        raise InvalidInput(
            f"there is no {tally.label} entry {entry!r}; choose one of {sorted(tally.entries)}"
        )
    before = load_state(db, campaign_id).tallies.get(tally.id, {}).get(entry, 0)
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
        ),
    )
    db.commit()
    return {"tally": tally.label, "entry": label, "before": before, "value": after}
