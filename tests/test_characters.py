"""What the player knows of the people they meet, and the Characters page that shows it."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.services.events import name_key
from tests.test_campaigns import _campaign_id, _place_pc
from tests.test_world import _state, _tool

PC = "Ada"


def _characters_section(page: str) -> str:
    """The known-people part of the Characters page, without the Log's history of the whole
    campaign, where every NPC's name still appears."""
    start = page.index('id="character-list"')
    known = page.index("People you know", start)
    return page[start : page.index("</section>", known)]


def test_meeting_an_npc_records_what_the_player_knows(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")

    known = _state(app, campaign_id).known
    assert set(known) == {name_key("Brin Gallowcap")}
    brin = known[name_key("Brin Gallowcap")]
    assert brin.location == "Glowcap Ring"
    # The NPC's own description is a GM record and may hold secrets, so meeting them does not
    # copy it. Only the observable traits are learned.
    assert brin.description == ""
    assert brin.personality


def test_a_secret_in_an_npc_description_is_not_shown_to_the_player(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _tool(
        app,
        campaign_id,
        "create_npc",
        {
            "name": "Vale",
            "location": "Glowcap Ring",
            "description": "A baker. He is secretly the cult's leader.",
        },
    )
    assert _state(app, campaign_id).known[name_key("Vale")].description == ""
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Secretly the cult" not in _characters_section(page)

    _tool(
        app,
        campaign_id,
        "update_npc_knowledge",
        {"name": "Vale", "description": "A flour-dusted baker with a quick laugh."},
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    characters = _characters_section(page)
    assert "A flour-dusted baker with a quick laugh." in characters
    assert "secretly the cult" not in characters


def test_an_npc_coming_to_the_player_is_remembered(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Quiet Camp", "description": "A quiet camp."},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": PC, "location": "Quiet Camp"},
    )
    assert _state(app, campaign_id).known == {}

    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Mara Thistledown", "location": "Quiet Camp"},
    )
    mara = _state(app, campaign_id).known[name_key("Mara Thistledown")]
    assert mara.location == "Quiet Camp"
    assert mara.personality == "warm and protective"


def test_knowledge_is_a_snapshot_and_keeps_the_last_known_place(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    before = _state(app, campaign_id).known[name_key("Brin Gallowcap")]

    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": PC, "location": "Mossbell Hollow"},
    )
    _tool(
        app,
        campaign_id,
        "update_npc",
        {"name": "Brin Gallowcap", "personality": "cold and watchful"},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Brin Gallowcap", "location": "Spore Bridge"},
    )
    brin = _state(app, campaign_id).known[name_key("Brin Gallowcap")]
    assert brin.personality == before.personality
    assert brin.location == "Glowcap Ring"

    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Brin Gallowcap", "location": "Mossbell Hollow"},
    )
    brin = _state(app, campaign_id).known[name_key("Brin Gallowcap")]
    assert brin.personality == "cold and watchful"
    assert brin.location == "Mossbell Hollow"


def test_update_npc_knowledge_records_a_relationship_a_place_and_a_rumor(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    result = _tool(
        app,
        campaign_id,
        "update_npc_knowledge",
        {
            "name": "Old Hob",
            "description": "A toll-keeper on the Spore Bridge.",
            "relationship": "wary of the party",
            "location": "Spore Bridge",
        },
    )
    assert result == {"updated": "npc knowledge", "name": "Old Hob"}
    hob = _state(app, campaign_id).known[name_key("Old Hob")]
    assert hob.description == "A toll-keeper on the Spore Bridge."
    assert hob.relationship == "wary of the party"
    assert hob.location == "Spore Bridge"
    # The player was never told a personality, so none is known.
    assert hob.personality == ""

    _tool(
        app,
        campaign_id,
        "update_npc_knowledge",
        {"name": "Old Hob", "relationship": "friendly, owes a debt"},
    )
    hob = _state(app, campaign_id).known[name_key("Old Hob")]
    assert hob.relationship == "friendly, owes a debt"
    assert hob.description == "A toll-keeper on the Spore Bridge."
    assert hob.location == "Spore Bridge"


def test_update_npc_knowledge_refuses_an_empty_change_or_unknown_place(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    empty = _tool(app, campaign_id, "update_npc_knowledge", {"name": "Old Hob"})
    assert "give at least one" in empty["error"]
    nowhere = _tool(
        app,
        campaign_id,
        "update_npc_knowledge",
        {"name": "Old Hob", "location": "Nowhere"},
    )
    assert "Nowhere" in nowhere["error"]


def test_a_removed_npc_is_forgotten(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    assert name_key("Brin Gallowcap") in _state(app, campaign_id).known

    _tool(app, campaign_id, "delete_npc", {"name": "Brin Gallowcap"})
    assert name_key("Brin Gallowcap") not in _state(app, campaign_id).known


def test_a_dismissed_hireling_joins_the_people_the_player_knows(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _tool(
        app,
        campaign_id,
        "hire_hireling",
        {"name": "Wick", "employer": PC, "wage": 1, "description": "A lantern-bearer."},
    )
    assert name_key("Wick") not in _state(app, campaign_id).known

    _tool(app, campaign_id, "dismiss_hireling", {"name": "Wick", "cause": "The pay ran out."})
    wick = _state(app, campaign_id).known[name_key("Wick")]
    # The hireling's own description stays a GM record, like any NPC's.
    assert wick.description == ""
    assert wick.location == "Glowcap Ring"


def test_characters_page_shows_known_people_and_their_last_known_place(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _tool(
        app,
        campaign_id,
        "update_npc_knowledge",
        {"name": "Brin Gallowcap", "relationship": "friendly, owes a debt"},
    )

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-view-target="characters"' in page
    assert 'data-view="characters"' in page
    assert 'type="search"' in page
    assert 'data-filter="#character-list"' in page
    characters = _characters_section(page)
    assert "Brin Gallowcap" in characters
    assert "friendly, owes a debt" in characters
    assert "Here at Glowcap Ring" in characters
    assert "Mara Thistledown" not in characters
    assert _state(app, campaign_id).known[name_key("Brin Gallowcap")].personality in characters

    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": PC, "location": "Mossbell Hollow"},
    )
    characters = _characters_section(client.get(f"/campaigns/{campaign_id}").text)
    assert "Brin Gallowcap" in characters
    assert "Last known: Glowcap Ring" in characters
    assert "Mara Thistledown" in characters
