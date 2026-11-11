"""Give each language model its own runtime context window.

The prompt budget used to be one environment-backed server setting. It is a property of the
model instead: a campaign uses the context size of the model it is currently talking to, and an
administrator can change that value without restarting the server.

Revision ID: 0015_llm_model_context_tokens
Revises: 0014_campaign_major_version
Create Date: 2026-11-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_llm_model_context_tokens"
down_revision: str | None = "0014_campaign_major_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_CONTEXT_TOKENS = 65536


def upgrade() -> None:
    op.add_column(
        "llm_model",
        sa.Column(
            "context_tokens",
            sa.Integer(),
            nullable=False,
            server_default=str(DEFAULT_CONTEXT_TOKENS),
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_model", "context_tokens")
