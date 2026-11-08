"""Let each campaign turn the rule set's inventory space on or off.

The column defaults to true, so existing campaigns keep the limits their rule set prescribes.
Off means capacity is not checked and no penalty item or full-pack drain is applied.

Revision ID: 0011_campaign_inventory_space
Revises: 0010_model_calculator
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_campaign_inventory_space"
down_revision: str | None = "0010_model_calculator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("use_inventory_space", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("campaign", "use_inventory_space")
