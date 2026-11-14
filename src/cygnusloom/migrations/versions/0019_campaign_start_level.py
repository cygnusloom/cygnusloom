"""Give each campaign a starting level for its new player characters.

The column defaults to 1, so existing campaigns start new characters at level 1. A rule set
without levels ignores it; the campaign's rule set supplies the highest level it allows.

Revision ID: 0019_campaign_start_level
Revises: 0018_campaign_survival_needs
Create Date: 2026-11-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_campaign_start_level"
down_revision: str | None = "0018_campaign_survival_needs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("start_level", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("campaign", "start_level")
