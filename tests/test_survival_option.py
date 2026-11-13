"""The per-campaign option that turns food, water, and rest privation on or off."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import NO_CAMPAIGN
from cygnusloom.db.models import Campaign
from cygnusloom.services import views
from cygnusloom.services.campaigns import set_campaign_survival_needs
from cygnusloom.services.events import load_state
from tests.conftest import csrf_token
from tests.factories import campaign_ready_fast
from tests.test_character_creation import _cairn_campaign_fast, _complete_form
from tests.test_clock_rest import _wound
from tests.test_combat import PC, _character, _fight
from tests.test_export_import import _export, _import, _imported_id, _target_app, _target_client
from tests.test_wwn_advancement import NAME, _campaign, _pc, _world_tools
from tests.test_wwn_travel import _setup as _travel_setup


def _flag(app: FastAPI, campaign_id: int) -> bool:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.use_survival_needs


def _owner_id(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.owner_id


def _set_needs(client: TestClient, campaign_id: int, enabled: bool):
    """Post the Settings checkbox. An unchecked box sends no field at all."""
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    data = {"csrf_token": token}
    if enabled:
        data["use_survival_needs"] = "1"
    return client.post(
        f"/campaigns/{campaign_id}/survival-needs",
        data=data,
        follow_redirects=False,
    )


def _disable(app: FastAPI, campaign_id: int) -> None:
    set_campaign_survival_needs(app.state.engine, _owner_id(app, campaign_id), campaign_id, False)


def _post_settings(client: TestClient, campaign_id: int, **data: str):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/settings",
        data={"csrf_token": token, **data},
        follow_redirects=False,
    )


def test_the_creation_page_offers_both_tracking_switches_checked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign_fast(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'name="use_inventory_space" value="1" checked' in page
    assert 'name="use_survival_needs" value="1" checked' in page
    assert page.index('id="options-title"') < page.index('name="use_survival_needs"')


def test_the_creation_form_stores_both_switches(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign_fast(client_factory)
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    fields = _complete_form(client, campaign_id)
    response = client.post(
        f"/campaigns/{campaign_id}/creation",
        data={
            "csrf_token": token,
            "name": "Mara",
            "pronouns": "she/her",
            **fields,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    campaign = _campaign_row(client.app, campaign_id)
    assert campaign.use_inventory_space is False
    assert campaign.use_survival_needs is False


def _campaign_row(app: FastAPI, campaign_id: int) -> Campaign:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign


def test_a_new_campaign_tracks_survival_needs_by_default(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    assert _flag(client.app, campaign_id) is True
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="use_survival_needs" value="1" checked' in page


def test_the_owner_turns_survival_needs_off_and_on(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)

    assert _set_needs(client, campaign_id, False).status_code == 303
    assert _flag(client.app, campaign_id) is False
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="use_survival_needs" value="1" checked' not in page
    assert 'name="use_survival_needs" value="1"' in page

    assert _set_needs(client, campaign_id, True).status_code == 303
    assert _flag(client.app, campaign_id) is True


def test_the_settings_form_saves_both_switches(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = _post_settings(
        client,
        campaign_id,
        model="fake/scripted",
        label="Contract Hollow",
        use_inventory_space="1",
    )

    assert response.status_code == 303
    campaign = _campaign_row(client.app, campaign_id)
    assert campaign.use_inventory_space is True
    assert campaign.use_survival_needs is False


def test_a_member_cannot_turn_survival_needs_off(
    client_factory: Callable[[], TestClient],
):
    from tests.test_multiplayer import _joined_campaign

    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    token = csrf_token(guest, "/campaigns")
    response = guest.post(
        f"/campaigns/{campaign_id}/survival-needs",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert _flag(owner.app, campaign_id) is True


def test_a_wwn_rest_spends_nothing_when_survival_needs_are_off(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    tools.set_stat_max(NAME, "strain", 10, cause="a test")
    tools.harm_character(NAME, "hp", 2, "a test")
    tools.drop_item(NAME, "Rations, One Week")
    if any(i.name == "Waterskin" for i in _pc(app, campaign_id).items):
        tools.drop_item(NAME, "Waterskin")
    _disable(app, campaign_id)

    result = tools.rest("night", reason="camp")

    after = _pc(app, campaign_id)
    assert result["held_back"] == []
    assert NAME in result["recovered"]
    assert after.marks.get("privation_food") is None
    assert after.marks.get("privation_water") is None
    assert not after.harm.get("strain")


def test_a_cairn_rest_eats_no_ration_when_survival_needs_are_off(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    if not any(i.name == "Rations" for i in _character(app, campaign_id, PC).items):
        tools.give_item(PC, "Rations")
    rations = [i for i in _character(app, campaign_id, PC).items if i.name == "Rations"]
    _wound(app, campaign_id)
    _disable(app, campaign_id)

    result = tools.rest("night", reason="camp")

    after = _character(app, campaign_id, PC)
    assert result["held_back"] == []
    assert PC in result["recovered"]
    assert [i for i in after.items if i.name == "Rations"] == rations
    assert "deprived" not in after.marks


def test_wwn_travel_demands_no_supplies_when_survival_needs_are_off(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _travel_setup(client_factory)
    _disable(app, campaign_id)

    result = tools.travel(NAME, "Far", "plains", days=2)

    assert result["survival_needs"] is False
    assert result["supplies"]["food"] == 0
    assert result["supplies"]["water"] == 0
    assert result["supplies"]["firewood"] == 0
    assert result["privation"] == {}


def test_the_gm_state_says_survival_needs_are_not_tracked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)

    text = views.world_state_text(state, NO_CAMPAIGN, track_survival_needs=False)

    assert "Survival needs are not tracked in this campaign" in text


def test_export_and_import_round_trip_the_survival_needs_flag(
    tmp_path, settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    assert _set_needs(client, campaign_id, False).status_code == 303

    response = _export(client, campaign_id)

    assert response.json()["campaign"]["use_survival_needs"] is False
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    new_id = _imported_id(_import(target_client, response.content))
    assert _flag(target_app, new_id) is False


def test_an_old_export_without_the_flag_imports_with_survival_needs_tracked(
    tmp_path, settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _export(client, campaign_id).json()
    document["campaign"].pop("use_survival_needs")

    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    new_id = _imported_id(_import(target_client, json.dumps(document).encode()))
    assert _flag(target_app, new_id) is True
