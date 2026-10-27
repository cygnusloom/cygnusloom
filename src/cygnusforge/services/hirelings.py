"""Hirelings' wages. A hireling is paid each new day from its employer's currency. An employer who
cannot pay is recorded as unpaid and nothing is taken; the GM decides what the hireling does."""

from __future__ import annotations

from sqlmodel import Session

from cygnusforge.services.events import (
    CurrencyChanged,
    HirelingUnpaid,
    load_state,
    name_key,
    record,
)


def pay_wages(db: Session, campaign_id: int, currency: str) -> None:
    """Pay every hireling's wage for one day. Each employer's balance is tracked across its
    hirelings, so a second hireling is not paid from currency the first one already took. It does not
    commit."""
    state = load_state(db, campaign_id)
    balances: dict[str, int] = {}
    for hireling in [c for c in state.characters.values() if c.hireling]:
        if hireling.wage <= 0:
            continue
        key = name_key(hireling.employer)
        employer = state.characters.get(key)
        if employer is None or employer.dead:
            record(
                db,
                campaign_id,
                HirelingUnpaid(
                    name=hireling.name,
                    employer=hireling.employer,
                    wage=hireling.wage,
                    currency=currency,
                ),
            )
            continue
        if key not in balances:
            balances[key] = employer.features.get(currency, 0)
        if balances[key] < hireling.wage:
            record(
                db,
                campaign_id,
                HirelingUnpaid(
                    name=hireling.name,
                    employer=employer.name,
                    wage=hireling.wage,
                    currency=currency,
                ),
            )
            continue
        balances[key] -= hireling.wage
        record(
            db,
            campaign_id,
            CurrencyChanged(
                name=employer.name,
                feature=currency,
                amount=-hireling.wage,
                cause=f"wages for {hireling.name}",
            ),
        )
