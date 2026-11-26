"""Let administrators price uploaded adventure sources per model.

The upload used to cost one credit per 100,000 characters of extracted text. Each model now
chooses either a multiple of its credits per reply, charged once per document, or a flat fee per
100,000 characters. The defaults keep the old rate's price for a typical published adventure:
three replies per document at one credit per reply.

Revision ID: 0023_llm_document_pricing
Revises: 0022_campaign_document
Create Date: 2026-11-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_llm_document_pricing"
down_revision: str | None = "0022_campaign_document"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_model",
        sa.Column("document_pricing", sa.String(length=16), nullable=False, server_default="turns"),
    )
    op.add_column(
        "llm_model",
        sa.Column("document_multiple", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "llm_model",
        sa.Column("document_per_100k", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("llm_model", "document_per_100k")
    op.drop_column("llm_model", "document_multiple")
    op.drop_column("llm_model", "document_pricing")
