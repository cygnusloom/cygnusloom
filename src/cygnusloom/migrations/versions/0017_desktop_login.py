"""One-time sign-ins handed from a host browser to the desktop app.

Revision ID: 0017_desktop_login
Revises: 0016_user_usage_filter
Create Date: 2026-11-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_desktop_login"
down_revision: str | None = "0016_user_usage_filter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "desktop_login",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("flow", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=200), nullable=False),
        sa.Column("next_path", sa.String(length=500), nullable=False, server_default="/campaigns"),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("auth_time", sa.DateTime(), nullable=True),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_desktop_login_flow", "desktop_login", ["flow"], unique=True)
    op.create_index("ix_desktop_login_user_id", "desktop_login", ["user_id"], unique=False)
    op.create_index("ix_desktop_login_code_hash", "desktop_login", ["code_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_desktop_login_code_hash", table_name="desktop_login")
    op.drop_index("ix_desktop_login_user_id", table_name="desktop_login")
    op.drop_index("ix_desktop_login_flow", table_name="desktop_login")
    op.drop_table("desktop_login")
