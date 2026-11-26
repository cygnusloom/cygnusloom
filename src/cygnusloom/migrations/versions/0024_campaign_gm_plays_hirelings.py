"""Give each campaign the option to let the GM play the hirelings' turns.

The column defaults to false, so existing campaigns keep letting the player play each hireling's
turn. A campaign that turns it on waits on a Start button for a hireling's turn, as it does for an
NPC's, and the GM acts for the hireling until the turn ends.

Revision ID: 0024_campaign_gm_plays_hirelings
Revises: 0023_llm_document_pricing
Create Date: 2026-11-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_campaign_gm_plays_hirelings"
down_revision: str | None = "0023_llm_document_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("gm_plays_hirelings", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("campaign", "gm_plays_hirelings")
