"""Share campaigns with other accounts through memberships and join codes.

Revision ID: 0006_campaign_sharing
Revises: 0005_campaign_table_genres
Create Date: 2026-11-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_campaign_sharing"
down_revision: str | None = "0005_campaign_table_genres"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_campaign_member_campaign_user"),
    )
    op.create_index("ix_campaign_member_campaign_id", "campaign_member", ["campaign_id"])
    op.create_index("ix_campaign_member_user_id", "campaign_member", ["user_id"])

    op.create_table(
        "campaign_invite",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id"), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_campaign_invite_campaign_id", "campaign_invite", ["campaign_id"])
    op.create_index("ix_campaign_invite_code", "campaign_invite", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_campaign_invite_code", table_name="campaign_invite")
    op.drop_index("ix_campaign_invite_campaign_id", table_name="campaign_invite")
    op.drop_table("campaign_invite")
    op.drop_index("ix_campaign_member_user_id", table_name="campaign_member")
    op.drop_index("ix_campaign_member_campaign_id", table_name="campaign_member")
    op.drop_table("campaign_member")
