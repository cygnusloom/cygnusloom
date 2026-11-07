"""Record the game version a campaign was last upgraded to.

A blank value marks a campaign created before version tracking, so the page offers an upgrade.

Revision ID: 0007_campaign_game_version
Revises: 0006_campaign_sharing
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_campaign_game_version"
down_revision: str | None = "0006_campaign_sharing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("game_version", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("campaign", "game_version")
