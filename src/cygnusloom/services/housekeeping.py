"""Scene housekeeping: what an earlier scene left behind at a place.

The engine only computes and words the tasks; the GM decides and acts. Bodies and fight debris
are facts in the log, so a task disappears when the GM updates the place's description, moves
or deletes a body, or lets enough story time pass. Nothing here names a particular game."""

from __future__ import annotations

from dataclasses import dataclass

from cygnusloom.content.library import HousekeepingRules
from cygnusloom.services.state import NPC_KIND, PC_KIND, CampaignState, Character, Place


@dataclass(frozen=True)
class SceneTask:
    """One thing the GM may consider before narrating a place."""

    kind: str
    place: str
    text: str
    names: tuple[str, ...] = ()
    count: int = 0
    days: int = 0


def scene_tasks(
    state: CampaignState,
    place: Place,
    rules: HousekeepingRules | None,
    *,
    day: int | None = None,
) -> list[SceneTask]:
    """Housekeeping the GM may consider at `place`, oldest scene first. Returns an empty list
    when the rule set asks for none or nothing has waited long enough."""
    if rules is None:
        return []
    today = state.clock.day if day is None else day
    tasks: list[SceneTask] = []
    bodies = _bodies(state, place, rules, today)
    if bodies is not None:
        tasks.append(bodies)
    if rules.fight_debris is not None and place.fight_day is not None:
        days = max(0, today - place.fight_day)
        if days >= rules.fight_debris.after_days:
            tasks.append(
                _task(
                    "fight_debris",
                    place,
                    rules.fight_debris.task,
                    names=(),
                    count=0,
                    days=days,
                )
            )
    return tasks


def body_count(state: CampaignState, place: Place) -> int:
    """How many body records are at the place."""
    return sum(
        1
        for character in state.characters.values()
        if _is_body(character) and character.location == place.name
    )


def _bodies(
    state: CampaignState,
    place: Place,
    rules: HousekeepingRules,
    today: int,
) -> SceneTask | None:
    if rules.bodies is None:
        return None
    found: list[tuple[int, Character]] = []
    for character in state.characters.values():
        if character.location != place.name or not _is_body(character):
            continue
        if character.fell_day is None:
            continue
        days = max(0, today - character.fell_day)
        if days >= rules.bodies.after_days:
            found.append((days, character))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], item[1].name.casefold()))
    names = tuple(_body_name(character, days) for days, character in found)
    oldest = max(days for days, _ in found)
    task = _task(
        "bodies",
        place,
        rules.bodies.task,
        names=names,
        count=len(found),
        days=oldest,
    )
    if any(character.kind == PC_KIND and not character.hireling for _, character in found):
        task = SceneTask(
            kind=task.kind,
            place=task.place,
            text=(
                f"{task.text} A player character's body should not be deleted; move it or leave "
                "it where the story needs it."
            ),
            names=task.names,
            count=task.count,
            days=task.days,
        )
    return task


def _is_body(character: Character) -> bool:
    """Whether the record is a body, not merely a character out of a fight. A dead character is
    always a body. A defeated ordinary NPC is a body because the rule sets leave ordinary NPCs
    dead at defeat; an important NPC or a player character may only be mortally wounded."""
    if character.dead:
        return True
    if not character.defeated:
        return False
    if character.kind == PC_KIND and not character.hireling:
        return False
    return not character.important


def _body_name(character: Character, days: int) -> str:
    if character.kind == NPC_KIND and character.large_group:
        name = f"the fallen of {character.name}"
    else:
        name = character.name
    return f"{name} ({_days_text(days)})"


def _days_text(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "1 day"
    return f"{days} days"


def _task(
    kind: str,
    place: Place,
    template: str,
    *,
    names: tuple[str, ...],
    count: int,
    days: int,
) -> SceneTask:
    text = template.format(
        place=place.name,
        names=", ".join(names),
        count=count,
        days=_days_text(days),
    )
    return SceneTask(
        kind=kind,
        place=place.name,
        text=text,
        names=names,
        count=count,
        days=days,
    )
