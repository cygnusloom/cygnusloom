"""The full Settings page, the per-viewer thinking preference, and the GM's setting tool."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, User
from cygnusloom.services.campaigns import list_messages
from cygnusloom.services.events import ToolRan, decode, events_since
from tests.conftest import SECRET, csrf_token, make_app
from tests.factories import (
    answer,
    campaign_ready_fast,
    create_campaign,
    create_character,
    send_message,
)
from tests.test_multiplayer import _joined_campaign

SETTINGS_MODEL = "ollama/test-model"


def _campaign(app: FastAPI, campaign_id: int) -> Campaign:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        db.expunge(campaign)
        return campaign


def _user(app: FastAPI, user_id: int) -> User:
    with Session(app.state.engine) as db:
        user = db.get(User, user_id)
        assert user is not None
        db.expunge(user)
        return user


def _post_settings(client: TestClient, campaign_id: int, **data: str):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/settings",
        data={"csrf_token": token, **data},
        follow_redirects=False,
    )


def _post_thinking(client: TestClient, campaign_id: int, enabled: bool):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    data = {"csrf_token": token}
    if enabled:
        data["show_internal_thinking"] = "1"
    return client.post(
        f"/campaigns/{campaign_id}/thinking",
        data=data,
        follow_redirects=False,
    )


def _tool_script(name: str, arguments: dict[str, object]) -> str:
    return "__FAKE__:" + json.dumps(
        {
            "turns": [
                {
                    "rounds": [[{"name": name, "arguments": arguments}]],
                    "text": "The world answers.",
                }
            ]
        }
    )


def _tool_runs(app: FastAPI, campaign_id: int) -> list[ToolRan]:
    with Session(app.state.engine) as db:
        events = [decode(row) for row in events_since(db, campaign_id)]
    return [event for event in events if isinstance(event, ToolRan)]


def _tool_results(runs: list[ToolRan]) -> list[str]:
    return [
        message["content"]
        for run in runs
        for message in run.messages
        if message.get("role") == "tool"
    ]


def _reply_id(app: FastAPI, campaign_id: int) -> int:
    return next(m.id for m in list_messages(app.state.engine, campaign_id) if m.role == "assistant")


def test_the_settings_view_replaces_the_dialog_for_owner_and_member(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)

    for client in (owner, guest):
        page = client.get(f"/campaigns/{campaign_id}").text
        assert 'data-view-target="settings"' in page
        assert 'data-view="settings" aria-labelledby="settings-title"' in page
        assert 'id="view-settings"' in page
        assert "Your view" in page
        assert '<dialog id="dialog-settings"' not in page
        assert 'data-dialog-target="dialog-settings"' not in page
        assert 'class="composer-settings"' not in page


def test_the_owner_saves_model_name_and_inventory_in_one_form(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = _post_settings(
        client,
        campaign_id,
        model=SETTINGS_MODEL,
        label="Renamed game",
        use_inventory_space="1",
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}#view-settings"
    campaign = _campaign(app, campaign_id)
    assert (campaign.model, campaign.name, campaign.use_inventory_space) == (
        SETTINGS_MODEL,
        "Renamed game",
        True,
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'value="ollama/test-model" checked' in page
    assert 'name="label" value="Renamed game"' in page
    assert 'name="use_inventory_space" value="1" checked' in page


def test_unchecking_inventory_space_turns_it_off(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = _post_settings(client, campaign_id, model="fake/scripted", label="Contract Hollow")

    assert response.status_code == 303
    assert _campaign(app, campaign_id).use_inventory_space is False
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="use_inventory_space" value="1" checked' not in page


def test_only_the_owner_may_save_campaign_settings(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, guest, campaign_id, _ = _joined_campaign(client_factory)

    response = _post_settings(
        guest,
        campaign_id,
        model=SETTINGS_MODEL,
        label="Guest's game",
        use_inventory_space="1",
    )

    assert response.status_code == 404
    campaign = _campaign(app, campaign_id)
    assert (campaign.model, campaign.name) == ("fake/scripted", "Contract Hollow")


def test_saving_settings_refuses_an_unknown_model_or_a_blank_name(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    assert (
        _post_settings(client, campaign_id, model="openai/gpt-x", label="Still named").status_code
        == 400
    )
    assert (
        _post_settings(client, campaign_id, model="fake/scripted", label="   ").status_code == 400
    )
    campaign = _campaign(app, campaign_id)
    assert (campaign.model, campaign.name) == ("fake/scripted", "Contract Hollow")


def test_export_and_delete_are_owner_only_and_members_see_a_summary(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)

    owner_page = owner.get(f"/campaigns/{campaign_id}").text
    member_page = guest.get(f"/campaigns/{campaign_id}").text

    for markup in (
        f'href="/campaigns/{campaign_id}/export"',
        f'action="/campaigns/{campaign_id}/delete"',
        'name="confirm"',
        'class="danger"',
    ):
        assert markup in owner_page
        assert markup not in member_page

    assert "The campaign owner chooses the model for every reply." in member_page
    assert "Inventory space is tracked" in member_page
    assert "Contract Hollow" in member_page


def test_the_thinking_preference_is_per_account_and_controls_reply_details(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    reply_id = _reply_id(app, campaign_id)
    context_url = f"/campaigns/{campaign_id}/messages/{reply_id}/context"
    transcript_url = f"/campaigns/{campaign_id}/messages/{reply_id}/transcript"

    owner_page = owner.get(f"/campaigns/{campaign_id}").text
    assert 'class="transcript"' in owner_page
    assert 'data-show-thinking="0"' in owner_page
    assert context_url not in owner_page
    assert owner.get(context_url).status_code == 404
    assert owner.get(transcript_url).status_code == 404

    assert _post_thinking(owner, campaign_id, True).status_code == 303
    owner_page = owner.get(f"/campaigns/{campaign_id}").text
    assert 'class="transcript show-hidden"' in owner_page
    assert 'data-show-thinking="1"' in owner_page
    assert context_url in owner_page
    assert owner.get(context_url).status_code == 200
    assert owner.get(transcript_url).status_code == 200

    guest_page = guest.get(f"/campaigns/{campaign_id}").text
    assert 'data-show-thinking="0"' in guest_page
    assert context_url not in guest_page
    assert guest.get(context_url).status_code == 404

    assert _post_thinking(guest, campaign_id, True).status_code == 303
    assert guest.get(context_url).status_code == 200
    assert _user(app, _campaign(app, campaign_id).owner_id).show_internal_thinking is True

    assert _post_thinking(owner, campaign_id, False).status_code == 303
    assert owner.get(context_url).status_code == 404
    assert guest.get(context_url).status_code == 200


def test_saving_the_view_offers_a_dismiss_button_for_its_notice(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = _post_thinking(client, campaign_id, True)

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Your view is saved." in page
    assert "data-dismiss-flash" in page
    assert '<script src="/static/flash.js"></script>' in page
    # The handler is served with the page instead of depending on a page-specific script.
    script = client.get("/static/flash.js")
    assert script.status_code == 200
    assert 'closest(".flash")' in script.text


def test_single_player_settings_page_keeps_credit_text_out(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'solo.db'}",
        secret_key=SECRET,
        llm_models=("fake/scripted",),
        llm_default_model="fake/scripted",
        base_url="http://127.0.0.1:8000",
        single_player=True,
    )
    client = TestClient(make_app(settings))
    campaign_id = create_campaign(client, label="Solo")
    assert create_character(client, campaign_id).status_code == 303
    answer(client, campaign_id, "continue")

    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'id="view-settings"' in page
    assert "Show internal GM thinking" in page
    assert "credits per reply" not in page
    assert "This model is free." not in page
    assert 'href="/credits"' not in page


def test_the_owner_can_change_inventory_space_through_the_gm(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    assert _campaign(app, campaign_id).use_inventory_space is True

    response = send_message(
        client,
        campaign_id,
        _tool_script("set_campaign_setting", {"setting": "inventory_space", "value": False}),
    )

    assert response.status_code == 303
    assert _campaign(app, campaign_id).use_inventory_space is False
    runs = _tool_runs(app, campaign_id)
    assert runs[-1].render() == (
        "Campaign setting changed: Inventory space is no longer tracked; "
        "anything reasonable can be carried."
    )
    result = json.loads(_tool_results(runs)[-1])
    assert result == {
        "setting": "inventory_space",
        "value": False,
        "detail": ("Inventory space is no longer tracked; anything reasonable can be carried."),
    }


def test_a_member_cannot_change_settings_through_the_gm(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, guest, campaign_id, _ = _joined_campaign(client_factory)

    response = send_message(
        guest,
        campaign_id,
        _tool_script("set_campaign_setting", {"setting": "inventory_space", "value": False}),
    )

    assert response.status_code == 303
    assert _campaign(app, campaign_id).use_inventory_space is True
    results = _tool_results(_tool_runs(app, campaign_id))
    assert any("only the campaign's owner can change its settings" in result for result in results)


def test_the_gm_cannot_change_the_model_through_the_tool(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = send_message(
        client,
        campaign_id,
        _tool_script("set_campaign_setting", {"setting": "model", "value": False}),
    )

    assert response.status_code == 303
    assert _campaign(app, campaign_id).model == "fake/scripted"
    results = _tool_results(_tool_runs(app, campaign_id))
    assert any("must be one of: inventory_space" in result for result in results)


def test_a_hidden_settings_view_is_refreshed_after_a_turn(
    client_factory: Callable[[], TestClient],
):
    """A setting the GM changes mid-turn is current when the player opens Settings."""
    client, _ = campaign_ready_fast(client_factory)
    script = client.get("/static/turn_stream.js").text
    assert 'current.dataset.view === "settings" && !current.hidden' in script
