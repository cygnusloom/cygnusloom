"""Add the administrator-managed language model catalog.

Revision ID: 0002_llm_models
Revises: 0001_initial
Create Date: 2026-10-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_llm_models"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_model",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", "model", name="uq_llm_model_provider_model"),
    )
    op.create_index("ix_llm_model_provider", "llm_model", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_llm_model_provider", table_name="llm_model")
    op.drop_table("llm_model")
