"""Store the genres of generation table a campaign narrowed itself to.

Revision ID: 0005_campaign_table_genres
Revises: 0004_campaign_setting
Create Date: 2026-11-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_campaign_table_genres"
down_revision: str | None = "0004_campaign_setting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("table_genres", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("campaign", "table_genres")
