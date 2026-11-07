"""Add credit balances, per-model turn prices, and measured token usage.

The pricing unit is one AI GM reply: `credits_per_turn` is deducted from the campaign owner's
balance when a reply is saved. `llm_usage` stores what each model call actually used, grouped by
`reply_event_id`, so administrators can tune that price from real measurements.

Revision ID: 0008_credits
Revises: 0007_campaign_game_version
Create Date: 2026-11-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_credits"
down_revision: str | None = "0007_campaign_game_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("credit_balance", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "llm_model",
        sa.Column("credits_per_turn", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "llm_model",
        sa.Column("stream_usage", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "campaign",
        sa.Column("force_credit_charges", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reply_event_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_llm_usage_campaign_id", "llm_usage", ["campaign_id"])
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_reply_event_id", "llm_usage", ["reply_event_id"])
    op.create_index("ix_llm_usage_model", "llm_usage", ["model"])
    op.create_table(
        "credit_entry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_credit_entry_user_id", "credit_entry", ["user_id"])
    op.create_index("ix_credit_entry_campaign_id", "credit_entry", ["campaign_id"])
    op.create_index("ix_credit_entry_event_id", "credit_entry", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_credit_entry_event_id", table_name="credit_entry")
    op.drop_index("ix_credit_entry_campaign_id", table_name="credit_entry")
    op.drop_index("ix_credit_entry_user_id", table_name="credit_entry")
    op.drop_table("credit_entry")
    op.drop_index("ix_llm_usage_model", table_name="llm_usage")
    op.drop_index("ix_llm_usage_reply_event_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_user_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_campaign_id", table_name="llm_usage")
    op.drop_table("llm_usage")
    op.drop_column("campaign", "force_credit_charges")
    op.drop_column("llm_model", "stream_usage")
    op.drop_column("llm_model", "credits_per_turn")
    op.drop_column("user", "credit_balance")
