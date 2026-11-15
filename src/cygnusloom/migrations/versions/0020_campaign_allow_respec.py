"""Give each campaign the option to let players respec their characters.

The column defaults to false, so existing campaigns keep their characters as they are.
A campaign that turns it on lets a player rebuild their character's sheet from scratch,
refunding levels, experience, and advancement picks.

Revision ID: 0020_campaign_allow_respec
Revises: 0019_campaign_start_level
Create Date: 2026-11-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_campaign_allow_respec"
down_revision: str | None = "0019_campaign_start_level"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("allow_respec", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("campaign", "allow_respec")
