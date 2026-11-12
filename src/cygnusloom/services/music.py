"""Background music cues: the mood a scene should play, and the licensed pack metadata.

The GM chooses a mood, never a track. A track is chosen in the browser from the manifest, so the
model never has to carry the catalog in its prompt. The engine also derives a baseline mood from
the encounter and the player's surroundings, which keeps combat music working even when the GM
does not cue it.

The manifest and the audio files live together under `web/static/music`, so the browser can fetch
both without a second endpoint. The manifest is data, not campaign state; a cue is an event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cygnusloom.content.library import ClockRules
from cygnusloom.services.state import PC_KIND, CampaignState, Character, name_key

# The moods the GM may choose. Keep this list small: it is shown in the tool schema and the
# prompt, and a large vocabulary makes the model guess.
MOODS = (
    "calm",
    "exploration",
    "tension",
    "combat",
    "victory",
    "sorrow",
    "mystery",
    "horror",
    "social",
    "silence",
)
MOOD_LABELS = {
    "calm": "Calm",
    "exploration": "Exploration",
    "tension": "Tension",
    "combat": "Combat",
    "victory": "Victory",
    "sorrow": "Sorrow",
    "mystery": "Mystery",
    "horror": "Horror",
    "social": "Social",
    "silence": "Silence",
}
INTENSITIES = (1, 2, 3)
ALLOWED_LICENSES = frozenset(
    {
        "CC0-1.0",
        "CC-BY-3.0",
        "CC-BY-4.0",
        "CC-BY-SA-3.0",
        "CC-BY-SA-4.0",
        "PD",
    }
)

MUSIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "music"
MANIFEST_PATH = MUSIC_DIR / "manifest.json"

# World-map icons that suggest a place's mood. Unknown icons fall through to the default.
SOCIAL_ICONS = frozenset({"town", "village", "castle", "market", "tavern", "inn", "temple"})
MYSTERY_ICONS = frozenset({"ruins", "cave", "tomb", "crypt", "shrine"})


@dataclass(frozen=True)
class MusicView:
    """The music state a page should render."""

    mood: str
    intensity: int
    source: str

    @property
    def label(self) -> str:
        """The mood's player-facing name."""
        return MOOD_LABELS.get(self.mood, self.mood.title())


def scene_key(state: CampaignState) -> str:
    """The scene a GM cue belongs to. A cue expires when the encounter, place, or day-part
    changes, so a combat cue cannot linger after the fight and a town cue cannot follow the
    party into the wilderness."""
    pc = state.pc()
    location = pc.location if pc is not None and pc.location else ""
    encounter = state.encounter.map if state.encounter is not None else ""
    return f"{encounter}|{location}|{state.clock.day}|{state.clock.part}"


def derive_scene_mood(state: CampaignState, clock_rules: ClockRules | None = None) -> MusicView:
    """The mood the scene itself implies. This is the fallback and the hard override for combat
    and death, whether or not the GM has cued anything."""
    if state.encounter is not None:
        in_danger = any(
            character.kind == PC_KIND
            and (
                character.dead
                or character.defeated
                or character.dying_hours is not None
                or character.dying_rounds is not None
            )
            for character in state.characters.values()
        )
        return MusicView("combat", 3 if in_danger else 2, "auto")

    pcs = state.pcs()
    if pcs and all(
        pc.out_of_play or pc.dying_hours is not None or pc.dying_rounds is not None for pc in pcs
    ):
        return MusicView("sorrow", 2, "auto")

    icon = _location_icon(state, state.pc())
    if icon in SOCIAL_ICONS:
        return MusicView("social", 1, "auto")
    if icon in MYSTERY_ICONS:
        return MusicView("mystery", 2, "auto")
    if _is_night(state, clock_rules):
        return MusicView("tension", 1, "auto")
    return MusicView("exploration", 1, "auto")


def music_view(
    state: CampaignState,
    *,
    clock_rules: ClockRules | None = None,
) -> MusicView:
    """The effective music state: the GM's cue while its scene lasts, otherwise the derived
    mood. An encounter and a fallen player character override a stale cue. Whether a player
    hears anything is a browser preference, not campaign state."""
    derived = derive_scene_mood(state, clock_rules)
    cue = state.music
    if state.encounter is not None:
        if (
            cue is not None
            and cue.scene == scene_key(state)
            and cue.mood == "combat"
            and cue.intensity in INTENSITIES
        ):
            return MusicView("combat", max(derived.intensity, cue.intensity), "gm")
        return derived
    if derived.mood == "sorrow":
        return derived
    if cue is not None and cue.scene == scene_key(state):
        if cue.mood == "combat":
            # Combat is over; the cue belongs to the fight, not to the scene after it.
            return derived
        if cue.mood in MOODS and cue.intensity in INTENSITIES:
            return MusicView(cue.mood, cue.intensity, cue.source)
    return derived


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Read the track manifest. The app serves this same file to the browser."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(data: Any, *, music_dir: Path = MUSIC_DIR) -> list[str]:
    """Problems that would make the pack unusable or the attribution incomplete. An empty list
    means every track has an id, title, artist, file, source, license, and mood tag."""
    if not isinstance(data, dict):
        return ["the manifest must be a JSON object"]
    if data.get("version") != 1:
        return ["the manifest version must be 1"]
    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return ["the manifest must list at least one track"]
    problems: list[str] = []
    seen: set[str] = set()
    for index, track in enumerate(tracks):
        label = f"track {index + 1}"
        if not isinstance(track, dict):
            problems.append(f"{label} must be an object")
            continue
        track_id = str(track.get("id") or "").strip()
        if not track_id:
            problems.append(f"{label} has no id")
        elif track_id in seen:
            problems.append(f"{label} repeats the id {track_id!r}")
        else:
            seen.add(track_id)
        for field in ("title", "artist", "file", "attribution", "source_url", "license_url"):
            if not str(track.get(field) or "").strip():
                problems.append(f"{label} has no {field}")
        license_name = str(track.get("license") or "")
        if license_name not in ALLOWED_LICENSES:
            problems.append(f"{label} has an unsupported license {license_name!r}")
        moods = track.get("moods")
        if not isinstance(moods, list) or not moods:
            problems.append(f"{label} has no moods")
        else:
            unknown = [mood for mood in moods if mood not in MOODS]
            if unknown:
                problems.append(f"{label} has unknown moods: {', '.join(map(str, unknown))}")
        intensity = track.get("intensity")
        if (
            not isinstance(intensity, list)
            or len(intensity) != 2
            or not all(isinstance(value, int) for value in intensity)
            or not 1 <= intensity[0] <= intensity[1] <= 3
        ):
            problems.append(f"{label} intensity must be [low, high] within 1 to 3")
        if not isinstance(track.get("loop"), bool):
            problems.append(f"{label} loop must be true or false")
        file_name = str(track.get("file") or "")
        if file_name and not (music_dir / file_name).is_file():
            problems.append(f"{label} file {file_name!r} is missing")
    return problems


def _location_icon(state: CampaignState, pc: Character | None) -> str:
    """The first icon on the player's place or one of its parents, or an empty string."""
    if pc is None or not pc.location:
        return ""
    place = state.places.get(name_key(pc.location))
    seen: set[str] = set()
    while place is not None:
        if place.icon:
            return place.icon
        if not place.parent:
            return ""
        parent_key = name_key(place.parent)
        if parent_key in seen:
            return ""
        seen.add(parent_key)
        place = state.places.get(parent_key)
    return ""


def _is_night(state: CampaignState, clock_rules: ClockRules | None) -> bool:
    if clock_rules is None or not clock_rules.parts:
        return False
    return state.clock.part >= len(clock_rules.parts) - 1
