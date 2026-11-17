from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, User
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services import campaign_files
from cygnusloom.services import modules as modules_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state
from cygnusloom.services.prompts import opening_prompt
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library, csrf_token, make_app
from tests.factories import (
    choose_campaign_options,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    creation_fields,
    send_message,
    sign_in,
    signed_in_client,
)

TWIN_LAKES = "twin-lakes"
HERMIT = "the-hermit-of-ash-hollow"


def _account_id(client: TestClient) -> int:
    with Session(client.app.state.engine) as session:
        return session.exec(select(User).where(User.email == "ada@example.com")).one().id


def _tools(client: TestClient, campaign_id: int) -> WorldTools:
    library = client.app.state.content
    rules = library.rule_sets["cairn2e"]
    return WorldTools(
        client.app.state.engine,
        campaign_id,
        {},
        random.Random(1),
        rules.character,
        plus=rules.plus,
        modules=library.modules,
        table_genres=("generic", "fantasy"),
    )


def test_twin_lakes_is_a_campaign_module_with_its_own_setting():
    library = content_library()
    module = library.modules[TWIN_LAKES]
    assert module.kind == "campaign"
    assert module.world_template.id == TWIN_LAKES
    assert library.world_templates[TWIN_LAKES].module == TWIN_LAKES
    # The source material is the world itself, not a second set of story cards.
    assert module.body == ""
    assert "sandbox mystery" in module.gm_notes
    assert any(place.id == "deadmill" for place in module.world_template.locations)
    # A module's setting is not offered as a standalone world.
    assert all(world.id != TWIN_LAKES for world in library.offered_worlds("cairn2e"))


def test_a_mini_module_loads_a_synthetic_setting():
    module = content_library().modules[HERMIT]
    assert module.kind == "mini"
    assert module.world_template.id == HERMIT
    assert [npc.id for npc in module.world_template.npcs] == ["mara-ash"]
    assert [place.id for place in module.world_template.locations] == ["ash-hollow-hut"]
    assert "Mara Ash saw the miller's son" in module.body


def test_the_opening_prompt_uses_a_module_start_and_hooks():
    library = content_library()
    text = opening_prompt(library.world_templates[TWIN_LAKES], library.modules[TWIN_LAKES])
    assert "Tideborn" in text
    assert "campaign module" in text
    assert "Deadmill" not in text


def test_a_campaign_module_can_supply_the_setting_and_is_recorded(
    client_factory,
):
    client = signed_in_client(client_factory)
    token = csrf_token(client, "/campaigns/new")
    response = client.post(
        "/campaigns/new",
        data={
            "csrf_token": token,
            "label": "Twin Lakes run",
            "world_template": "",
            "rule_set": "cairn2e",
            "module": TWIN_LAKES,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with Session(client.app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
    assert campaign is not None
    assert campaign.module == TWIN_LAKES
    assert campaign.world_template == TWIN_LAKES


def test_the_gm_can_list_and_read_a_campaign_module(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    tools = _tools(client, campaign_id)
    cards = tools.list_modules(kind="campaign")["modules"]
    assert [card["id"] for card in cards] == [TWIN_LAKES]
    text = tools.lookup_module(TWIN_LAKES)["text"]
    assert "Module: Twin Lakes (campaign)" in text
    assert "Opening hooks" in text
    assert "ordinary world entries" in text
    used = tools.use_module(TWIN_LAKES, "the opening rumour")
    assert "Twin Lakes" in used["text"]


def test_a_mini_module_is_used_once_and_then_no_longer_offered(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    tools = _tools(client, campaign_id)
    assert [card["id"] for card in tools.list_modules(kind="mini")["modules"]] == [HERMIT]
    with pytest.raises(InvalidInput, match="use_module"):
        tools.lookup_module(HERMIT)
    text = tools.use_module(HERMIT, "a rumour on the river road")["text"]
    assert "Mara Ash" in text
    assert "hush-money" in text
    with Session(client.app.state.engine) as session:
        state = load_state(session, campaign_id)
    assert HERMIT in state.used_modules
    assert tools.list_modules(kind="mini")["modules"] == []
    assert "Mara Ash" in tools.lookup_module(HERMIT)["text"]
    with pytest.raises(InvalidInput, match="already used"):
        tools.use_module(HERMIT)


def test_a_mini_module_cannot_be_attached_as_a_campaign_module(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    with pytest.raises(InvalidInput, match="mini module"):
        modules_service.set_attached_modules(
            client.app.state.engine,
            client.app.state.content,
            _account_id(client),
            campaign_id,
            [HERMIT],
        )


def test_the_starting_module_stays_attached_when_the_owner_saves_modules(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    modules_service.set_attached_modules(
        client.app.state.engine,
        client.app.state.content,
        _account_id(client),
        campaign_id,
        [],
    )
    with Session(client.app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
        state = load_state(session, campaign_id)
    assert campaign is not None and campaign.module == TWIN_LAKES
    assert state.modules == {}


def test_the_campaign_page_shows_the_module_without_its_cards(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    choose_campaign_options(client, campaign_id, "cairn2e")
    fields = creation_fields(client, campaign_id)
    create_character_direct(client, campaign_id, fields=fields)
    close_opening(client, campaign_id)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Story modules" in page
    assert "Twin Lakes" in page
    assert "sandbox mystery" not in page


def test_a_turn_with_a_campaign_module_puts_its_brief_in_the_state_prompt(
    client_factory, fake
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    choose_campaign_options(client, campaign_id, "cairn2e")
    fields = creation_fields(client, campaign_id)
    create_character_direct(client, campaign_id, fields=fields)
    close_opening(client, campaign_id)
    response = send_message(
        client,
        campaign_id,
        'Look around. __FAKE__:{"turns": [{"text": "The lake is quiet."}]}',
    )
    assert response.status_code == 303, response.text
    system = "\n".join(
        message.get("content", "")
        for messages in fake.calls
        for message in messages
        if message.get("role") == "system"
    )
    assert "Module: Twin Lakes (campaign)" in system
    assert "ordinary world entries" in system


def test_an_export_keeps_the_starting_module_and_import_restores_it(
    client_factory, settings, tmp_path
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    payload = client.get(f"/campaigns/{campaign_id}/export").content
    document = json.loads(payload)
    assert document["campaign"]["module"] == TWIN_LAKES

    target = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'target.db'}",
        auto_create_schema=True,
    )
    app = make_app(target, FakeProvider())
    with TestClient(app) as target_client:
        sign_in(target_client)
        token = csrf_token(target_client, "/campaigns")
        response = target_client.post(
            "/campaigns/import",
            data={"csrf_token": token},
            files={"file": ("campaign.json", payload, "application/json")},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        imported_id = int(response.headers["location"].rsplit("/", 1)[1])
        with Session(app.state.engine) as session:
            imported = session.get(Campaign, imported_id)
    assert imported is not None
    assert imported.module == TWIN_LAKES
    assert imported.world_template == TWIN_LAKES


def test_an_old_export_without_a_module_field_still_finds_the_embedded_module(
    client_factory,
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    payload = client.get(f"/campaigns/{campaign_id}/export").content
    document = json.loads(payload)
    document["campaign"].pop("module")
    library = client.app.state.content
    config = campaign_files._read_campaign(document["campaign"], library)
    assert config["module"] == TWIN_LAKES
