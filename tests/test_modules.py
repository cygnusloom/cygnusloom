from __future__ import annotations

import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.db.models import Campaign
from cygnusloom.services import modules as modules_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CampaignState, load_state
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library, csrf_token
from tests.factories import (
    choose_campaign_options,
    create_campaign_direct,
    create_character_direct,
    creation_fields,
    signed_in_client,
)
from tests.support import content_tree, write_files

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
    assert TWIN_LAKES not in library.world_templates
    assert library.setting_template(TWIN_LAKES) is world


def _mini_module_manifest(location_density: object) -> dict[str, object]:
    return {
        "type": "module",
        "id": "fits-anywhere",
        "kind": "mini",
        "name": "Fits Anywhere",
        "blurb": "A seed that can appear almost anywhere.",
        "categories": ["generic"],
        "tones": ["adventure"],
        "location_density": location_density,
        "body": "Bring the seed in wherever the party happens to be.",
    }


def _load_mini_module(tmp_path: Path, location_density: object):
    root = content_tree(tmp_path)
    write_files(
        root,
        {"modules/fits-anywhere/module.json": [_mini_module_manifest(location_density)]},
    )
    return ContentLibrary.load(root).modules["fits-anywhere"]


def test_a_module_can_name_every_density_it_fits(tmp_path: Path):
    module = _load_mini_module(tmp_path, ["low", "moderate", "dense"])
    assert module.location_density == ("low", "moderate", "dense")
    card = modules_service.module_card(module)
    assert card["location_density"] == ["low", "moderate", "dense"]
    assert len(card["location_density_notes"]) == 3
    assert "compact site" in card["location_density_notes"][0]
    assert "connected area" in card["location_density_notes"][2]


def test_a_single_density_from_an_old_manifest_still_loads(tmp_path: Path):
    module = _load_mini_module(tmp_path, "low")
    assert module.location_density == ("low",)


@pytest.mark.parametrize(
    ("location_density", "message"),
    [
        ([], "at least one"),
        (["low", "low"], "only once"),
        (["anywhere"], "unknown location density"),
    ],
)
def test_a_bad_location_density_list_is_rejected(
    tmp_path: Path, location_density: object, message: str
):
    with pytest.raises(ContentError, match=message):
        _load_mini_module(tmp_path, location_density)


def test_list_modules_offers_a_fitting_seed_once(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template=TWIN_LAKES, rule_set="cairn2e")
    tools = _tools(client, campaign_id)
    cards = tools.list_modules()["modules"]
    assert [card["id"] for card in cards] == [HERMIT]
    assert cards[0]["categories"] == ["fantasy"]
    assert cards[0]["tones"] == ["grim", "mystery"]
    assert cards[0]["location_density"] == ["low"]
    assert "compact site" in cards[0]["location_density_notes"][0]
    with pytest.raises(InvalidInput, match="use_module"):
        tools.lookup_module(HERMIT)
    module = client.app.state.content.modules[HERMIT]
    text = tools.use_module(HERMIT, "a rumour on the river road")["text"]
    assert text == module.body
    with Session(client.app.state.engine) as session:
        state = load_state(session, campaign_id)
    assert HERMIT in state.used_modules
    assert tools.list_modules()["modules"] == []
    assert tools.lookup_module(HERMIT)["text"] == text
    with pytest.raises(InvalidInput, match="already used"):
        tools.use_module(HERMIT)


def test_campaign_tones_and_explicit_filters_narrow_the_seed_pool(client_factory):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template=TWIN_LAKES, rule_set="cairn2e")
    tools = _tools(client, campaign_id)
    assert tools.list_modules(tone="mystery")["modules"][0]["id"] == HERMIT
    assert tools.list_modules(tone="relaxing")["modules"] == []
    with pytest.raises(InvalidInput, match="no module tone"):
        tools.list_modules(tone="cozy")

    quiet_id = create_campaign_direct(
        client, label="Quiet", world_template=TWIN_LAKES, rule_set="cairn2e"
    )
    choose_campaign_options(client, quiet_id, "cairn2e")
    create_character_direct(
        client,
        quiet_id,
        fields=creation_fields(client, quiet_id),
        table_tones=["relaxing"],
    )
    quiet = _tools(client, quiet_id)
    assert quiet.list_modules()["modules"] == []


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
