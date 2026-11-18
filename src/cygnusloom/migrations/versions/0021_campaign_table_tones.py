"""Let each campaign choose the tones of its mini modules.

The column is blank by default, so existing campaigns keep the rule set's own tone selection.
A campaign stores the player's subset as JSON, alongside the creative categories in
`table_genres`.

Revision ID: 0021_campaign_table_tones
Revises: 0020_campaign_allow_respec
Create Date: 2026-11-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_campaign_table_tones"
down_revision: str | None = "0020_campaign_allow_respec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("table_tones", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("campaign", "table_tones")
