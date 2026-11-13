"""Let each campaign turn food, water, and rest privation on or off.

The column defaults to true, so existing campaigns keep tracking survival needs.
Off means a rest spends no rations or water and records no privation.

Revision ID: 0018_campaign_survival_needs
Revises: 0017_desktop_login
Create Date: 2026-11-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_campaign_survival_needs"
down_revision: str | None = "0017_desktop_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("use_survival_needs", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("campaign", "use_survival_needs")
