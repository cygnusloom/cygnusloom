"""Keep the provider ID token on a session so sign-out can present it as id_token_hint.

Revision ID: 0003_oidc_id_token
Revises: 0002_llm_models
Create Date: 2026-10-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_oidc_id_token"
down_revision: str | None = "0002_llm_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_session", sa.Column("oidc_id_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_session", "oidc_id_token")
