"""Give each campaign its contract major version.

Existing rows are stamped 3, not 4: a campaign created by 3.x must not silently become playable
under 4.0's changed content and event contract. The row and its log stay intact, and export still
works.

Revision ID: 0014_campaign_major_version
Revises: 0013_user_show_internal_thinking
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_campaign_major_version"
down_revision: str | None = "0013_user_show_internal_thinking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("major_version", sa.Integer(), nullable=False, server_default="3"),
    )
    op.execute("UPDATE campaign SET major_version = 3")


def downgrade() -> None:
    op.drop_column("campaign", "major_version")
