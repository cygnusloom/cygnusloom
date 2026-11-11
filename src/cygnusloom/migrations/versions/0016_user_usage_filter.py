"""Remember the measured-usage date filter for each administrator.

The filter is a view preference on the account, not campaign state, so it is not exported with
a campaign. Existing accounts start on All time.

Revision ID: 0016_user_usage_filter
Revises: 0015_llm_model_context_tokens
Create Date: 2026-11-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_user_usage_filter"
down_revision: str | None = "0015_llm_model_context_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "usage_filter_mode",
            sa.String(length=8),
            nullable=False,
            server_default="all",
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "usage_filter_start",
            sa.String(length=10),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "usage_filter_end",
            sa.String(length=10),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "usage_filter_end")
    op.drop_column("user", "usage_filter_start")
    op.drop_column("user", "usage_filter_mode")
