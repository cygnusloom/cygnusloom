"""Random names to start from: a campaign title and a player character's name."""

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
FIRST_NAMES = (
    "Ada",
    "Bram",
    "Cass",
    "Dorin",
    "Elowen",
    "Fenn",
    "Greta",
    "Hollis",
    "Idris",
    "Jory",
    "Kestrel",
    "Lio",
    "Mara",
    "Nell",
    "Oren",
    "Pim",
    "Quill",
    "Rhea",
    "Sable",
    "Tamsin",
    "Ulric",
    "Vesna",
    "Wren",
    "Yara",
)
LAST_NAMES = (
    "Ashgrove",
    "Briarwick",
    "Copperfield",
    "Dunmere",
    "Eversong",
    "Fairweather",
    "Goldhollow",
    "Haverly",
    "Ironvale",
    "Kettleby",
    "Larkspur",
    "Marrow",
    "Northcott",
    "Oakhaven",
    "Pellworth",
    "Ravensdale",
    "Thistledown",
    "Vance",
)


def random_campaign_name(rng: random.Random) -> str:
    return f"The {rng.choice(CAMPAIGN_ADJECTIVES)} {rng.choice(CAMPAIGN_NOUNS)}"


def random_character_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
