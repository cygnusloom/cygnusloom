"""Scene housekeeping: suggestions about bodies and fight debris for the GM to consider."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.character import NO_CAMPAIGN
from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import (
    HousekeepingRules,
    HousekeepingTaskRules,
    _load_housekeeping,
)
from cygnusloom.services import world
from cygnusloom.services.events import (
    CharacterDied,
    ClockAdvanced,
    EncounterEnded,
    EncounterStarted,
    LocationUpdated,
    name_key,
    record,
)
from cygnusloom.services.housekeeping import body_count, scene_tasks
from cygnusloom.services.state import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    Clock,
    Place,
)
from tests.conftest import content_library
from tests.factories import campaign_ready_fast, db, open_campaign, rules_for, tools_for

CAIRN = content_library().rule_sets["cairn2e"]
PLACE = "The Old Mill"


def _rules(
    *,
    body_after: int = 1,
    debris_after: int = 1,
    body_task: str = "Bodies at {place}: {names}. {count} bodies, oldest {days}.",
    debris_task: str = "A fight ended at {place} {days} ago.",
) -> HousekeepingRules:
    return HousekeepingRules(
        bodies=HousekeepingTaskRules(after_days=body_after, task=body_task),
        fight_debris=HousekeepingTaskRules(after_days=debris_after, task=debris_task),
    )


def _place(*, fight_day: int | None = None) -> Place:
    return Place(name=PLACE, description="A mill by the river.", fight_day=fight_day)


def _character(
    name: str = "Vuk",
    *,
    kind: str = NPC_KIND,
    dead: bool = False,
    defeated: bool = True,
    important: bool = False,
    fell_day: int | None = 1,
    large_group: bool = False,
    hireling: bool = False,
    location: str | None = PLACE,
) -> Character:
    return Character(
        kind=kind,
        name=name,
        location=location,
        description="",
        personality="",
        accent="",
        quirks=[],
        dead=dead,
        defeated=defeated,
        important=important,
        fell_day=fell_day,
        large_group=large_group,
        hireling=hireling,
    )


def _state(*characters: Character, day: int = 3, place: Place | None = None) -> CampaignState:
    place = place or _place()
    return CampaignState(
        places={name_key(place.name): place},
        characters={name_key(character.name): character for character in characters},
        clock=Clock(day=day),
    )


def test_a_body_waits_until_enough_days_have_passed() -> None:
    state = _state(_character(fell_day=1), day=3)
    tasks = scene_tasks(state, _place(), _rules(body_after=1))

    assert len(tasks) == 1
    assert tasks[0].kind == "bodies"
    assert tasks[0].count == 1
    assert tasks[0].days == 2
    assert tasks[0].names == ("Vuk (2 days)",)
    assert "Bodies at The Old Mill: Vuk (2 days). 1 bodies, oldest 2 days." in tasks[0].text

    fresh = _state(_character(fell_day=2), day=2)
    assert scene_tasks(fresh, _place(), _rules(body_after=1)) == []


def test_a_body_with_no_recorded_fall_day_is_left_alone() -> None:
    state = _state(_character(fell_day=None), day=5)
    assert scene_tasks(state, _place(), _rules()) == []


def test_a_defeated_player_character_is_not_a_body_but_a_dead_one_is() -> None:
    defeated_pc = _character("Ada", kind=PC_KIND, dead=False, defeated=True)
    state = _state(defeated_pc, day=3)
    assert scene_tasks(state, _place(), _rules()) == []

    dead_pc = _character("Ada", kind=PC_KIND, dead=True, defeated=True)
    state = _state(dead_pc, day=3)
    tasks = scene_tasks(state, _place(), _rules())
    assert len(tasks) == 1
    assert "A player character's body should not be deleted" in tasks[0].text


def test_a_defeated_important_npc_is_not_a_body() -> None:
    state = _state(_character(important=True, defeated=True), day=3)
    assert scene_tasks(state, _place(), _rules()) == []


def test_a_defeated_hireling_is_a_body() -> None:
    state = _state(_character("Wick", kind=PC_KIND, hireling=True, defeated=True), day=3)
    tasks = scene_tasks(state, _place(), _rules())
    assert [task.kind for task in tasks] == ["bodies"]
    assert "Wick (2 days)" in tasks[0].text


def test_a_large_group_is_worded_as_the_fallen() -> None:
    state = _state(_character("The Pack", large_group=True), day=3)
    tasks = scene_tasks(state, _place(), _rules())
    assert tasks[0].names == ("the fallen of The Pack (2 days)",)


def test_fight_debris_waits_for_its_threshold_and_uses_the_place_description() -> None:
    state = _state(day=4, place=_place(fight_day=2))
    tasks = scene_tasks(state, _place(fight_day=2), _rules(debris_after=1))

    assert [task.kind for task in tasks] == ["fight_debris"]
    assert tasks[0].days == 2
    assert tasks[0].text == "A fight ended at The Old Mill 2 days ago."

    same_day = _state(day=2, place=_place(fight_day=2))
    assert scene_tasks(same_day, _place(fight_day=2), _rules(debris_after=1)) == []


def test_no_rule_set_means_no_tasks() -> None:
    state = _state(_character(), day=5)
    assert scene_tasks(state, _place(fight_day=1), None) == []
    assert body_count(state, _place()) == 1


def test_the_core_housekeeping_definition_is_inherited_and_valid() -> None:
    assert CAIRN.housekeeping is not None
    assert CAIRN.housekeeping.bodies is not None
    assert CAIRN.housekeeping.bodies.after_days == 1
    assert CAIRN.housekeeping.fight_debris is not None


def test_the_housekeeping_loader_rejects_bad_placeholders_and_thresholds() -> None:
    with pytest.raises(ContentError, match="unknown placeholder"):
        _load_housekeeping(
            [
                Definition(
                    "housekeeping",
                    "bad",
                    {
                        "type": "housekeeping",
                        "id": "bad",
                        "bodies": {"after_days": 1, "task": "Bodies at {place}: {who}."},
                    },
                )
            ],
            "test",
        )
    with pytest.raises(ContentError, match="at least 0"):
        _load_housekeeping(
            [
                Definition(
                    "housekeeping",
                    "bad",
                    {
                        "type": "housekeeping",
                        "id": "bad",
                        "bodies": {"after_days": -1, "task": "Bodies at {place}."},
                    },
                )
            ],
            "test",
        )
    with pytest.raises(ContentError, match="give 'bodies'"):
        _load_housekeeping(
            [Definition("housekeeping", "empty", {"type": "housekeeping", "id": "empty"})],
            "test",
        )


def test_death_and_encounter_end_record_the_campaign_day() -> None:
    state = _state(_character(fell_day=None), day=4)
    CharacterDied(name="Vuk", cause="a wolf").apply(state, 1)
    assert state.characters[name_key("Vuk")].fell_day == 4

    EncounterStarted(map="Mill Yard", location=PLACE).apply(state, 2)
    EncounterEnded(reason="the wolf is down").apply(state, 3)
    assert state.places[name_key(PLACE)].fight_day == 4

    LocationUpdated(name=PLACE, description="The blood is washed away.").apply(state, 4)
    assert state.places[name_key(PLACE)].fight_day is None


def test_navigation_reports_housekeeping_and_the_turn_orientation_lists_it(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    with db(app) as session:
        record(session, campaign_id, CharacterDied(name="Brin Gallowcap", cause="a wolf"))
        record(
            session,
            campaign_id,
            ClockAdvanced(day=3, part=0, part_name="morning", new_day=True),
        )
        session.commit()
    tools = tools_for(app, campaign_id)

    result = tools.move_actor("Ada", "Glowcap Ring")

    assert result["moved"] == "Ada"
    assert any("Brin Gallowcap" in line for line in result["housekeeping"])

    state = open_campaign(app, campaign_id)
    text = world.world_state_text(
        state,
        NO_CAMPAIGN,
        housekeeping=rules_for().housekeeping,
    )
    assert "Housekeeping suggestions at Glowcap Ring:" in text
    assert "Brin Gallowcap (dead body)" in text

    clean = tools.move_actor("Ada", "Mossbell Hollow")
    assert "housekeeping" not in clean
