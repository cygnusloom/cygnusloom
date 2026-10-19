"""Random campaign titles."""

from __future__ import annotations

import random

CAMPAIGN_ADJECTIVES = (
    "Ashen",
    "Brass",
    "Crooked",
    "Drowned",
    "Emerald",
    "Foxglove",
    "Gilded",
    "Hollow",
    "Ivory",
    "Juniper",
    "Lantern",
    "Moonlit",
    "Rusted",
    "Silent",
    "Thistle",
    "Umber",
    "Velvet",
    "Winter",
)
CAMPAIGN_NOUNS = (
    "Archive",
    "Bridge",
    "Crossing",
    "Ferry",
    "Garden",
    "Harbor",
    "Lighthouse",
    "Market",
    "Orchard",
    "Road",
    "Shrine",
    "Tower",
    "Vale",
    "Warren",
    "Well",
)


def random_campaign_name(rng: random.Random) -> str:
    return f"The {rng.choice(CAMPAIGN_ADJECTIVES)} {rng.choice(CAMPAIGN_NOUNS)}"
