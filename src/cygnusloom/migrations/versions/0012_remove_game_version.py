"""Drop the campaign's recorded game version.

Campaigns always load the running game's rules and content, so there is nothing to upgrade and
no version to track. The log's campaign.upgraded events stay for old logs and exports.

Revision ID: 0012_remove_game_version
Revises: 0011_campaign_inventory_space
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_remove_game_version"
down_revision: str | None = "0011_campaign_inventory_space"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("campaign", "game_version")


def downgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("game_version", sa.String(length=64), nullable=False, server_default=""),
    )
