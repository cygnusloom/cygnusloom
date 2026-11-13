"""Per-campaign switches that services and rule hooks share."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cygnusloom.db.models import Campaign


def survival_needs_enabled(db: Session, campaign_id: int) -> bool:
    """Whether this campaign tracks food, water, and rest privation. A campaign whose row is
    missing keeps the rule set's behavior, so a caller outside a stored campaign is unaffected."""
    campaign = db.get(Campaign, campaign_id)
    return campaign is None or campaign.use_survival_needs
