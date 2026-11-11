"""What the player knows of the people they meet, and the Characters page that shows it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import NO_CAMPAIGN
from cygnusloom.llm.base import ToolRequest
from cygnusloom.services import world
from cygnusloom.services.events import (
    CharacterDied,
    CharacterSurrendered,
    Struck,
    name_key,
    record,
)
from cygnusloom.services.tools import ToolRun, run_tool
from tests.test_campaigns import _campaign_id, _place_pc
from tests.test_world import CAIRN_TOOLS, _ctx, _state

PC = "Ada"


def _tool(app: FastAPI, campaign_id: int, name: str, arguments: dict[str, Any]) -> dict:
    """Run a Cairn tool: the characters-page tests need its hireling tools."""
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool(CAIRN_TOOLS, request, None, _ctx(app, campaign_id)))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _characters_section(page: str) -> str:
    """The known-people part of the Characters page, without the Log's history of the whole
    campaign, where every NPC's name still appears."""
    start = page.index('id="character-list"')
    known = page.index("People you know", start)
    return page[start : page.index("</section>", known)]


def _fallen_section(page: str) -> str:
    """The Fallen part of the Characters page."""
    start = page.index('id="characters-fallen"')
    return page[start : page.index("</section>", start)]


def _record(app: FastAPI, campaign_id: int, event: Any) -> None:
    """Append an event to the campaign log as play would."""
    with Session(app.state.engine) as db:
        record(db, campaign_id, event)
        db.commit()


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
    _tool(
        app,
        campaign_id,
        "describe_character",
        {"name": "Vale", "summary": "A flour-dusted baker with a quick laugh."},
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

    summary = "A toll-keeper with a lantern and a long memory."
    _tool(app, campaign_id, "describe_character", {"name": "Old Hob", "summary": summary})
    assert _state(app, campaign_id).known[name_key("Old Hob")].summary == summary

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
    assert hob.summary == summary


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


def test_a_removed_npc_is_forgotten(client_factory: Callable[[], TestClient], app: FastAPI):
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

    summary = "A lantern-bearer who keeps to the back roads."
    _tool(
        app,
        campaign_id,
        "describe_character",
        {"name": "Wick", "summary": summary},
    )
    _tool(app, campaign_id, "dismiss_hireling", {"name": "Wick", "cause": "The pay ran out."})
    wick = _state(app, campaign_id).known[name_key("Wick")]
    # The hireling's own description stays a GM record, like any NPC's, but the summary the
    # player read while Wick was in the company stays with them.
    assert wick.description == ""
    assert wick.summary == summary
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
    summary = "A sharp-eyed forager who trades mushroom caps and gossip."
    _tool(
        app,
        campaign_id,
        "describe_character",
        {"name": "Brin Gallowcap", "summary": summary},
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
    assert summary in characters
    known = _state(app, campaign_id).known[name_key("Brin Gallowcap")]
    assert known.personality
    assert known.personality not in characters
    for quirk in known.quirks:
        assert quirk not in characters

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


def test_describe_character_summarizes_the_player_character_and_hirelings(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    player_summary = "Ada reads the stars and keeps her promises."
    assert _tool(
        app, campaign_id, "describe_character", {"name": PC, "summary": player_summary}
    ) == {"described": PC}

    _tool(
        app,
        campaign_id,
        "hire_hireling",
        {"name": "Wick", "employer": PC, "wage": 1, "description": "A lantern-bearer."},
    )
    hireling_summary = "A quiet lantern-bearer who knows the old roads."
    assert _tool(
        app,
        campaign_id,
        "describe_character",
        {"name": "Wick", "summary": hireling_summary},
    ) == {"described": "Wick"}

    characters = _characters_section(client.get(f"/campaigns/{campaign_id}").text)
    assert player_summary in characters
    assert hireling_summary in characters
    # The GM's own record of the hireling stays off the page.
    assert "A lantern-bearer." not in characters


def test_an_npc_summary_is_kept_when_the_player_meets_them_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _tool(
        app,
        campaign_id,
        "update_npc",
        {"name": "Brin Gallowcap", "description": "The cult's secret go-between."},
    )
    summary = "A sharp-eyed forager who knows every path in the ring."
    _tool(
        app,
        campaign_id,
        "describe_character",
        {"name": "Brin Gallowcap", "summary": summary},
    )
    assert _state(app, campaign_id).known[name_key("Brin Gallowcap")].summary == summary

    # Coming back to Brin refreshes the traits the player sees but keeps the summary.
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": PC, "location": "Mossbell Hollow"},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": PC, "location": "Glowcap Ring"},
    )
    assert _state(app, campaign_id).known[name_key("Brin Gallowcap")].summary == summary

    page = client.get(f"/campaigns/{campaign_id}").text
    characters = _characters_section(page)
    assert summary in characters
    assert "The cult's secret go-between." not in page
    assert "cult" not in characters


def test_describe_character_refuses_a_blank_summary(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    blank = _tool(app, campaign_id, "describe_character", {"name": PC, "summary": "   "})
    assert "summary must be 1 to 600 characters" in blank["error"]


def test_the_gm_is_reminded_to_write_missing_character_summaries(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "Characters with no player-facing summary: Ada, Brin Gallowcap" in text
    assert "describe_character, with no GM-only secrets" in text

    _tool(app, campaign_id, "describe_character", {"name": PC, "summary": "The seeker."})
    _tool(
        app,
        campaign_id,
        "describe_character",
        {"name": "Brin Gallowcap", "summary": "A forager from the ring."},
    )
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "Characters with no player-facing summary" not in text


def test_a_defeated_npc_moves_to_the_fallen_group(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    summary = "A sharp-eyed forager who trades mushroom caps and gossip."
    _tool(
        app,
        campaign_id,
        "update_npc_knowledge",
        {"name": "Brin Gallowcap", "relationship": "friendly, owes a debt"},
    )
    _tool(app, campaign_id, "describe_character", {"name": "Brin Gallowcap", "summary": summary})
    _record(
        app,
        campaign_id,
        Struck(
            name="Brin Gallowcap",
            attacker="a wolf",
            weapon="bite",
            raw=8,
            armor=0,
            harm={"hp": 8},
            defeats=True,
            status="dead",
        ),
    )

    page = client.get(f"/campaigns/{campaign_id}").text
    living = _characters_section(page)
    fallen = _fallen_section(page)
    assert "Brin Gallowcap" not in living
    assert "Brin Gallowcap" in fallen
    assert '<span class="tag tag-dead">Fallen</span>' in fallen
    assert 'class="character-card is-dead"' in fallen
    assert "Fell at: Glowcap Ring" in fallen
    assert "friendly, owes a debt" in fallen
    assert summary in fallen


def test_a_dead_npc_shows_the_dead_badge(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _record(app, campaign_id, CharacterDied(name="Brin Gallowcap", cause="a wolf"))

    page = client.get(f"/campaigns/{campaign_id}").text
    living = _characters_section(page)
    fallen = _fallen_section(page)
    assert "Brin Gallowcap" not in living
    assert "Brin Gallowcap" in fallen
    assert '<span class="tag tag-dead">Dead</span>' in fallen
    assert 'class="character-card is-dead"' in fallen


def test_a_surrendered_npc_stays_with_the_living(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _record(app, campaign_id, CharacterSurrendered(name="Brin Gallowcap"))

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Brin Gallowcap" in _characters_section(page)
    assert 'id="characters-fallen"' not in page


def test_a_dead_player_character_stays_under_you_and_is_marked_dead(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")
    _record(app, campaign_id, CharacterDied(name=PC, cause="a wolf"))

    page = client.get(f"/campaigns/{campaign_id}").text
    start = page.index('id="characters-you"')
    you = page[start : page.index("</section>", start)]
    assert PC in you
    assert 'class="character-card is-dead"' in you
    assert '<span class="tag tag-dead">Dead</span>' in you
