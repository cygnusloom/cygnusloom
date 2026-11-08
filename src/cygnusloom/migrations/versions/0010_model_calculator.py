"""Store each model's cost-calculator prices and optional peak hours.

Revision ID: 0010_model_calculator
Revises: 0009_display_names
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_model_calculator"
down_revision: str | None = "0009_display_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_model",
        sa.Column(
            "calculator_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_model", "calculator_json")
