"""Move campaigns from the removed Latter Earth template to the blank world.

The `latter-earth` world template was built from the free book's setting, which
the CC0 SRD does not grant, so it has been replaced by `unwritten-world`. A
campaign row that still names the old template would fail to load, because no
world of that id is bundled any more.

Revision ID: 0004_unwritten_world
Revises: 0003_oidc_id_token
Create Date: 2026-11-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_unwritten_world"
down_revision: str | None = "0003_oidc_id_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "latter-earth"
NEW = "unwritten-world"


def _move(old: str, new: str) -> None:
    op.execute(
        sa.text("UPDATE campaign SET world_template = :new WHERE world_template = :old").bindparams(
            new=new, old=old
        )
    )


def upgrade() -> None:
    _move(OLD, NEW)


def downgrade() -> None:
    _move(NEW, OLD)
