"""Store the setting and story brief the player asks for when a campaign is created.

Revision ID: 0004_campaign_setting
Revises: 0003_oidc_id_token
Create Date: 2026-11-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_campaign_setting"
down_revision: str | None = "0003_oidc_id_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("setting", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("campaign", "setting")
