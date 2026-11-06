"""Initial schema for identity, sessions, campaigns, and the admin audit log.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("roles", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_issuer_subject"),
    )
    op.create_index("ix_user_issuer", "user", ["issuer"])
    op.create_index("ix_user_email", "user", ["email"])

    op.create_table(
        "campaign",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("world_template", sa.String(length=64), nullable=False),
        sa.Column("rule_set", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_campaign_owner_id", "campaign", ["owner_id"])

    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("campaign_id", "seq", name="uq_event_campaign_seq"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_event_campaign_id", "event", ["campaign_id"])

    op.create_table(
        "user_session",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("auth_time", sa.DateTime(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=False),
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])

    op.create_table(
        "admin_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("actor_email", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_admin_audit_actor_id", "admin_audit", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_actor_id", table_name="admin_audit")
    op.drop_table("admin_audit")
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_table("user_session")
    op.drop_index("ix_event_campaign_id", table_name="event")
    op.drop_table("event")
    op.drop_index("ix_campaign_owner_id", table_name="campaign")
    op.drop_table("campaign")
    op.drop_index("ix_user_email", table_name="user")
    op.drop_index("ix_user_issuer", table_name="user")
    op.drop_table("user")
