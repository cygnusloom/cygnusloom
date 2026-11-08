"""Let each account choose whether to see the GM's internal thinking.

The preference shows the GM's hidden Log lines and lets the player open the context and
transcript of each reply. It belongs to the account, not to a campaign, and starts off so a
new player sees the story without the GM's bookkeeping.

Revision ID: 0013_user_show_internal_thinking
Revises: 0012_remove_game_version
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_user_show_internal_thinking"
down_revision: str | None = "0012_remove_game_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "show_internal_thinking", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "show_internal_thinking")
