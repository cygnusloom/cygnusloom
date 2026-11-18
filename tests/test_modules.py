from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign
from cygnusloom.services import modules as modules_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CampaignState, load_state
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library, csrf_token
from tests.factories import (
    choose_campaign_options,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    creation_fields,
    send_message,
    signed_in_client,
)

TWIN_LAKES = "twin-lakes"
HERMIT = "the-hermit-of-ash-hollow"


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


def test_a_campaign_module_owns_its_setting():
    library = content_library()
    module = library.modules[TWIN_LAKES]
    assert module.kind == "campaign"
    assert module.world_template is not None
    world = module.world_template
    assert any(place.id == "deadmill" for place in world.locations)
    assert any(npc.id == "flavia" for npc in world.npcs)
    assert TWIN_LAKES not in library.world_templates
    assert library.setting_template(TWIN_LAKES) is world


def test_a_mini_module_is_only_prose_and_tags():
    module = content_library().modules[HERMIT]
    assert module.kind == "mini"
    assert module.world_template is None
    assert "The herbwife saw the miller's son" in module.body
    assert "Create the herbwife" in module.gm_notes
    assert "Mara" not in module.body and "Mara" not in module.gm_notes
    assert module.tags["genres"] == ("fantasy", "mystery")


def test_list_modules_offers_a_fitting_seed_once(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template=TWIN_LAKES, rule_set="cairn2e")
    tools = _tools(client, campaign_id)
    assert [card["id"] for card in tools.list_modules()["modules"]] == [HERMIT]
    with pytest.raises(InvalidInput, match="use_module"):
        tools.lookup_module(HERMIT)
    text = tools.use_module(HERMIT, "a rumour on the river road")["text"]
    assert "The herbwife saw the miller's son" in text
    assert "hush-money" in text
    assert "Mara" not in text
    with Session(client.app.state.engine) as session:
        state = load_state(session, campaign_id)
    assert HERMIT in state.used_modules
    assert tools.list_modules()["modules"] == []
    assert "The herbwife saw the miller's son" in tools.lookup_module(HERMIT)["text"]
    with pytest.raises(InvalidInput, match="already used"):
        tools.use_module(HERMIT)


def test_the_state_prompt_mentions_available_mini_modules():
    module = content_library().modules[HERMIT]
    campaign = Campaign(
        owner_id=1,
        name="Test",
        world_template=TWIN_LAKES,
        rule_set="cairn2e",
        model="fake/scripted",
    )
    state = CampaignState()
    text = modules_service.module_state_text(
        {HERMIT: module}, campaign, state, default_genres=("fantasy",)
    )
    assert "Mini modules available: 1" in text
    state.used_modules.add(HERMIT)
    assert (
        modules_service.module_state_text(
            {HERMIT: module}, campaign, state, default_genres=("fantasy",)
        )
        == ""
    )


def test_twin_lakes_is_a_campaign_module_in_the_new_campaign_picker(client_factory):
    client = signed_in_client(client_factory)
    page = client.get("/campaigns/new").text
    assert '<option value="twin-lakes"' in page
    assert 'data-campaign-module="1"' in page
    assert 'name="module"' not in page


def test_twin_lakes_can_start_a_campaign_as_a_world(client_factory):
    client = signed_in_client(client_factory)
    token = csrf_token(client, "/campaigns/new")
    response = client.post(
        "/campaigns/new",
        data={
            "csrf_token": token,
            "label": "Twin Lakes run",
            "world_template": TWIN_LAKES,
            "rule_set": "cairn2e",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with Session(client.app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
    assert campaign is not None
    assert campaign.world_template == TWIN_LAKES
    assert not hasattr(campaign, "module")


def test_an_available_mini_module_is_in_the_turn_prompt(client_factory, fake):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template=TWIN_LAKES, rule_set="cairn2e")
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
    assert "Mini modules available: 1" in system
