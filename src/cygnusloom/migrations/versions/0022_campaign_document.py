"""Store the source documents a campaign was created from.

An uploaded adventure is campaign data: its extracted text and section index live in the
database, never in the bundled content. Existing campaigns have none, so the table starts empty.

Revision ID: 0022_campaign_document
Revises: 0021_campaign_table_tones
Create Date: 2026-11-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_campaign_document"
down_revision: str | None = "0021_campaign_table_tones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_document",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("characters", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sections", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"]),
    )
    op.create_index("ix_campaign_document_campaign_id", "campaign_document", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_campaign_document_campaign_id", table_name="campaign_document")
    op.drop_table("campaign_document")
