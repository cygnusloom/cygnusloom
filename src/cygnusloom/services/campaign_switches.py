"""Per-campaign switches that services and rule hooks share."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cygnusloom.db.models import Campaign


def survival_needs_enabled(db: Session, campaign_id: int) -> bool:
    """Whether this campaign tracks food, water, and rest privation. A campaign whose row is
    missing keeps the rule set's behavior, so a caller outside a stored campaign is unaffected."""
    campaign = db.get(Campaign, campaign_id)
    return campaign is None or campaign.use_survival_needs


def gm_plays_hirelings(db: Session, campaign_id: int) -> bool:
    """Whether this campaign gives the hirelings' encounter turns to the GM. Off, as it is for
    a missing campaign row, the player plays each hireling's turn."""
    campaign = db.get(Campaign, campaign_id)
    return bool(campaign is not None and campaign.gm_plays_hirelings)
