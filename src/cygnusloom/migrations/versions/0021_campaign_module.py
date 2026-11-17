"""Record the campaign module a campaign started from.

The column defaults to an empty string, so existing campaigns keep their world and gain no
module card. Attached modules are event-sourced; this column is the creation-time choice.

Revision ID: 0021_campaign_module
Revises: 0020_campaign_allow_respec
Create Date: 2026-11-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_campaign_module"
down_revision: str | None = "0020_campaign_allow_respec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("module", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("campaign", "module")
