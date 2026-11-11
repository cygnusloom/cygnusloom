"""Hirelings in a leveled rule set keep their employer's level and advance on the player page."""

from __future__ import annotations

import pytest
from sqlmodel import Session

from cygnusloom.services import hireling_levels
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import PC_KIND, HirelingAdded, load_state, record
from cygnusloom.services.views import scene_view
from tests.conftest import csrf_token
from tests.test_campaigns import _signed_in
from tests.test_wwn_advancement import (
    NAME,
    WWN,
    _campaign,
    _ctx,
    _world_tools,
    wwn,
)

HIRELING = "Tobin"
WAGE = 2
CHOICES = {
    "templates": {"class": "warrior", "tradition": "no_tradition"},
    "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
}


def _world(app) -> object:
    return app.state.content.world_content("unwritten-world", WWN.id)


def _owner(app, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
        owner = state.owner_of(state.pc())
    assert owner is not None
    return owner


def _track(app, campaign_id: int) -> tuple[int, ...]:
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    rate = state.options.get("xp_rate") or next(iter(WWN.level_ups["level"].tracks))
    return WWN.level_ups["level"].tracks[rate]


def _character(app, campaign_id: int, name: str):
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    return state.characters[name.casefold()]


def _hireling_campaign(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.create_location("Yard", "A quiet yard.")
    tools.move_actor(NAME, "Yard")
    return app, campaign_id, tools


def test_a_hired_npc_is_queued_to_catch_up_to_the_employers_level(client_factory):
    app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.adjust_counter(NAME, "level", 2)
    tools.create_npc(HIRELING, "Yard", "A steady hand with a rope.")

    tools.hire_existing_npc(HIRELING, NAME, WAGE, character=CHOICES)

    hireling = _character(app, campaign_id, HIRELING)
    assert hireling.kind == PC_KIND
    assert hireling.hireling is True
    assert hireling.counters.get("level", 1) == 1
    track = _track(app, campaign_id)
    assert hireling.counters.get("xp", 0) == track[2]
    pages = level_up_service.pages_for_player(
        app.state.engine, campaign_id, WWN, _world(app), _owner(app, campaign_id)
    )
    assert [page.character for page in pages] == [HIRELING]
    assert pages[0].kind == level_up_service.LEVEL
    assert pages[0].summary is not None
    assert pages[0].summary["new_level"] == 2


def test_the_player_sees_the_hirelings_features_with_its_sheet(client_factory):
    app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.hire_hireling(HIRELING, NAME, WAGE, "A steady hand.", character=CHOICES)
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    scene = scene_view(state, WWN.character)

    assert [sheet.name for sheet in scene.hireling_sheets] == [HIRELING]
    sheet = scene.hireling_sheets[0]
    assert sheet.employer == NAME
    assert sheet.abilities
    assert any(line.startswith("Stats:") for line in sheet.sheet)
    assert any(line.startswith("Skills:") for line in sheet.sheet)


def test_the_player_levels_the_hireling_through_each_catch_up_level(client_factory):
    app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.adjust_counter(NAME, "level", 2)
    tools.create_npc(HIRELING, "Yard", "A steady hand with a rope.")
    tools.hire_existing_npc(HIRELING, NAME, WAGE, character=CHOICES)

    for expected in (2, 3):
        pages = level_up_service.pages_for_player(
            app.state.engine, campaign_id, WWN, _world(app), _owner(app, campaign_id)
        )
        assert any(
            page.character == HIRELING and page.kind == level_up_service.LEVEL for page in pages
        )
        level_up_service.apply_page(app.state.engine, campaign_id, WWN, _world(app), {}, HIRELING)
        assert _character(app, campaign_id, HIRELING).counters["level"] == expected

    pages = level_up_service.pages_for_player(
        app.state.engine, campaign_id, WWN, _world(app), _owner(app, campaign_id)
    )
    assert not any(
        page.character == HIRELING and page.kind == level_up_service.LEVEL for page in pages
    )


def test_a_hireling_without_a_class_is_refused(client_factory):
    _app, _campaign_id, tools = _hireling_campaign(client_factory)
    tools.create_npc(HIRELING, "Yard", "A steady hand with a rope.")

    with pytest.raises(InvalidInput, match="so they can gain levels"):
        tools.hire_existing_npc(HIRELING, NAME, WAGE, character={})


def test_the_player_character_level_queues_the_hirelings_next_level(client_factory):
    app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.hire_hireling(HIRELING, NAME, WAGE, "A steady hand.", character=CHOICES)
    wwn.gain_xp(NAME, 3, "a session", _ctx(tools))

    level_up_service.apply_page(app.state.engine, campaign_id, WWN, _world(app), {}, NAME)

    hireling = _character(app, campaign_id, HIRELING)
    track = _track(app, campaign_id)
    assert hireling.counters.get("level", 1) == 1
    assert hireling.counters.get("xp", 0) == track[1]
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert level_up_service.advancement_due_for_player(state, WWN, _owner(app, campaign_id))
    pages = level_up_service.pages_for_player(
        app.state.engine, campaign_id, WWN, _world(app), _owner(app, campaign_id)
    )
    assert any(page.character == HIRELING and page.kind == level_up_service.LEVEL for page in pages)

    level_up_service.apply_page(app.state.engine, campaign_id, WWN, _world(app), {}, HIRELING)
    assert _character(app, campaign_id, HIRELING).counters["level"] == 2


def test_a_message_is_refused_while_a_hireling_has_a_level_waiting(client_factory):
    _app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.adjust_counter(NAME, "level", 2)
    tools.create_npc(HIRELING, "Yard", "A steady hand with a rope.")
    tools.hire_existing_npc(HIRELING, NAME, WAGE, character=CHOICES)
    client = _signed_in(client_factory)

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": "Onward."},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "level up" in response.text.lower()


def test_the_advancement_page_lists_every_character_waiting(client_factory):
    _app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.hire_hireling(HIRELING, NAME, WAGE, "A steady hand.", character=CHOICES)
    wwn.gain_xp(NAME, 3, "a session", _ctx(tools))
    tools.adjust_counter(HIRELING, "xp", 3)
    client = _signed_in(client_factory)

    page = client.get(f"/campaigns/{campaign_id}/advancement")

    assert page.status_code == 200
    assert "Advancement waiting for:" in page.text
    assert NAME in page.text
    assert HIRELING in page.text


def test_the_advancement_page_can_submit_for_a_hireling(client_factory):
    app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.adjust_counter(NAME, "level", 2)
    tools.create_npc(HIRELING, "Yard", "A steady hand with a rope.")
    tools.hire_existing_npc(HIRELING, NAME, WAGE, character=CHOICES)
    client = _signed_in(client_factory)
    url = f"/campaigns/{campaign_id}/advancement?character={HIRELING}"

    page = client.get(url)
    assert page.status_code == 200
    token = csrf_token(client, url)
    response = client.post(
        f"/campaigns/{campaign_id}/advancement",
        data={"csrf_token": token, "character": HIRELING},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _character(app, campaign_id, HIRELING).counters["level"] == 2


def test_the_level_up_redirect_explains_the_hirelings_waiting_level(client_factory):
    _app, campaign_id, tools = _hireling_campaign(client_factory)
    tools.hire_hireling(HIRELING, NAME, WAGE, "A steady hand.", character=CHOICES)
    wwn.gain_xp(NAME, 3, "a session", _ctx(tools))
    client = _signed_in(client_factory)
    url = f"/campaigns/{campaign_id}/advancement"
    token = csrf_token(client, url)

    response = client.post(
        f"/campaigns/{campaign_id}/advancement",
        data={
            "csrf_token": token,
            # Spend the whole level's pools in one go, so the hireling's catch-up level is the
            # next page the player sees.
            "skill-skills-sneak": "1",
            "pick-focus-0": "focus_authority",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"] == url
    page = client.get(url)
    assert f"{NAME} reached level 2." in page.text
    assert f"Next: {HIRELING} has a hireling level-up waiting." in page.text
    assert f"Level Up: {HIRELING}" in page.text
    assert "Hireling" in page.text


def test_a_legacy_hireling_without_a_class_is_not_queued_for_a_level(client_factory):
    app, campaign_id, tools = _hireling_campaign(client_factory)
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            HirelingAdded(
                name=HIRELING,
                employer=NAME,
                wage=WAGE,
                location="Yard",
                description="A legacy hand.",
                personality="",
                accent="",
                quirks=[],
            ),
        )
        db.commit()
    tools.adjust_counter(NAME, "level", 1)

    assert hireling_levels.sync_employer(tools, NAME) == []

    hireling = _character(app, campaign_id, HIRELING)
    assert hireling.counters.get("xp", 0) == 0
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert not level_up_service.advancement_due_for_player(state, WWN, _owner(app, campaign_id))
