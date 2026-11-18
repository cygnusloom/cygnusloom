from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import TableOption
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
        {**rules.tables},
        random.Random(1),
        rules.character,
        plus=rules.plus,
        dice_tables={**rules.dice_tables},
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


def test_a_mini_module_is_prose_tags_and_rolled_placeholders():
    module = content_library().modules[HERMIT]
    assert module.kind == "mini"
    assert module.world_template is None
    assert "{{herbwife}} saw the miller's son" in module.body
    assert set(module.rolls) == {"herbwife", "grandchild", "miller", "miller_son"}
    assert set(module.rolls.values()) == {"names"}
    assert "Mara" not in module.body and "Mara" not in module.gm_notes
    assert module.tags["genres"] == ("fantasy", "mystery")


def test_list_modules_offers_a_fitting_seed_once(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template=TWIN_LAKES, rule_set="cairn2e")
    tools = _tools(client, campaign_id)
    assert [card["id"] for card in tools.list_modules()["modules"]] == [HERMIT]
    with pytest.raises(InvalidInput, match="use_module"):
        tools.lookup_module(HERMIT)
    used = tools.use_module(HERMIT, "a rumour on the river road")
    names = used["rolls"]
    herbwife = names["herbwife"]
    assert len(set(names.values())) == len(names) == 4
    assert all(name in used["text"] for name in names.values())
    assert "{{" not in used["text"]
    assert "hush-money" in used["text"]
    with Session(client.app.state.engine) as session:
        state = load_state(session, campaign_id)
    assert HERMIT in state.used_modules
    assert state.module_rolls[HERMIT] == names
    assert tools.list_modules()["modules"] == []
    again = tools.lookup_module(HERMIT)
    assert again["rolls"] == names
    assert herbwife in again["text"]
    with pytest.raises(InvalidInput, match="already used"):
        tools.use_module(HERMIT)


def test_module_rolls_keep_character_table_results_distinct():
    tables = {
        "names": (
            TableOption(name=None, description="Alder"),
            TableOption(name=None, description="Brin"),
        )
    }
    rolls = modules_service.resolve_rolls(
        {"first": "names", "second": "names"},
        tables,
        {},
        random.Random(1),
        plus="sum",
    )
    assert set(rolls.values()) == {"Alder", "Brin"}


def test_module_rolls_can_use_a_creative_table():
    rules = content_library().rule_sets["cairn2e"]
    rolls = modules_service.resolve_rolls(
        {"action": "action"},
        {},
        rules.dice_tables,
        random.Random(1),
        plus=rules.plus,
    )
    assert rolls["action"]


def test_a_missing_module_table_is_reported_together_with_the_campaign():
    with pytest.raises(InvalidInput, match="no-such-table"):
        modules_service.resolve_rolls(
            {"herbwife": "no-such-table"},
            {},
            {},
            random.Random(1),
            plus="sum",
        )


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
