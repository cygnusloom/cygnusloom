"""Give every account a player display name instead of an email address.

Older sign-ins could store the identity provider's email as the display name when the provider
sent no name. Other players and the GM then saw that address. This migration replaces blank or
email-shaped display names with a generated two-word name and a four-digit number. A name a
player chooses on the Account page is left alone.

Revision ID: 0009_display_names
Revises: 0008_credits
Create Date: 2026-11-07

"""

from __future__ import annotations

import random
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_display_names"
down_revision: str | None = "0008_credits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADJECTIVES = (
    "Amber",
    "Bold",
    "Bright",
    "Calm",
    "Clever",
    "Copper",
    "Distant",
    "Gentle",
    "Golden",
    "Green",
    "Hidden",
    "Honest",
    "Ivory",
    "Kind",
    "Lively",
    "Lucky",
    "Misty",
    "Quiet",
    "Silver",
    "Steady",
    "Swift",
    "Wandering",
    "Wild",
    "Wise",
)
NOUNS = (
    "Badger",
    "Beacon",
    "Cairn",
    "Comet",
    "Crane",
    "Dawn",
    "Ember",
    "Falcon",
    "Fern",
    "Fox",
    "Harbor",
    "Heron",
    "Lantern",
    "Lark",
    "Maple",
    "Moth",
    "Oak",
    "Otter",
    "Pine",
    "Raven",
    "Reed",
    "River",
    "Sparrow",
    "Willow",
)


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text('SELECT id, display_name FROM "user"')).mappings().all()
    picker = random.SystemRandom()
    for row in rows:
        name = (row["display_name"] or "").strip()
        if name and "@" not in name:
            continue
        replacement = (
            f"{picker.choice(ADJECTIVES)} {picker.choice(NOUNS)} {picker.randint(1000, 9999)}"
        )
        connection.execute(
            sa.text('UPDATE "user" SET display_name = :display_name WHERE id = :id'),
            {"display_name": replacement, "id": row["id"]},
        )


def downgrade() -> None:
    """Player names are user data; there is nothing to reverse."""
