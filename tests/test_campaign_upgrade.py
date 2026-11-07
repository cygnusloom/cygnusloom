from __future__ import annotations

import json
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom import __version__
from cygnusloom.db.models import Campaign, Event
from cygnusloom.services.events import CampaignUpgraded, ChatGM
from cygnusloom.services.views import world_state_text
from tests.conftest import csrf_token
from tests.factories import campaign_ready_fast, open_campaign, rules_for, send_message


def _campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    return campaign_ready_fast(client_factory)


def _set_version(client: TestClient, campaign_id: int, version: str) -> None:
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.game_version = version
        db.add(campaign)
        db.commit()


def _version(client: TestClient, campaign_id: int) -> str:
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.game_version


def _upgrade(client: TestClient, campaign_id: int):
    return client.post(
        f"/campaigns/{campaign_id}/upgrade",
        data={"csrf_token": csrf_token(client, f"/campaigns/{campaign_id}")},
        follow_redirects=False,
    )


def _upgrade_events(client: TestClient, campaign_id: int) -> list[Event]:
    with Session(client.app.state.engine) as db:
        return list(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id,
                    Event.kind == CampaignUpgraded.kind,
                )
            )
        )


def test_a_new_campaign_records_the_running_game_version(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign(client_factory)

    assert _version(client, campaign_id) == __version__
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Upgrade campaign" not in page


def test_an_older_campaign_offers_an_upgrade(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign(client_factory)
    _set_version(client, campaign_id, "3.0.0")

    page = client.get(f"/campaigns/{campaign_id}").text

    assert "Game updated." in page
    assert "3.0.0" in page
    assert __version__ in page
    assert "Upgrade campaign" in page
    list_page = client.get("/campaigns").text
    assert "Upgrade available" in list_page


def test_upgrading_records_the_version_and_tells_the_gm_out_of_character(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign(client_factory)
    _set_version(client, campaign_id, "3.0.0")

    response = _upgrade(client, campaign_id)

    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}"
    assert _version(client, campaign_id) == __version__
    state = open_campaign(client.app, campaign_id)
    assert state.upgrade is not None
    assert state.upgrade.from_version == "3.0.0"
    assert state.upgrade.to_version == __version__
    assert state.turn is None
    text = world_state_text(state, rules_for().campaign)
    assert "out of character" in text
    assert "3.0.0" in text
    assert __version__ in text
    with Session(client.app.state.engine) as db:
        replies = list(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id,
                    Event.kind == ChatGM.kind,
                )
            )
        )
    assert replies
    assert "upgraded" in json.loads(replies[-1].payload)["content"].lower()
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Upgrade campaign" not in page
    assert "You said:" in page


def test_upgrading_again_records_nothing_new(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign(client_factory)
    _set_version(client, campaign_id, "3.0.0")

    assert _upgrade(client, campaign_id).status_code == 303
    assert _upgrade(client, campaign_id).status_code == 303

    assert len(_upgrade_events(client, campaign_id)) == 1


def test_the_story_continues_normally_after_upgrading(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign(client_factory)
    _set_version(client, campaign_id, "3.0.0")
    assert _upgrade(client, campaign_id).status_code == 303

    script = json.dumps({"turns": [{"text": "The market stirs.\n\nHalta waits."}]})
    response = send_message(client, campaign_id, f"__FAKE__:{script}")

    assert response.status_code == 303, response.text
    state = open_campaign(client.app, campaign_id)
    assert state.turn is None
    with Session(client.app.state.engine) as db:
        replies = list(
            db.exec(
                select(Event)
                .where(Event.campaign_id == campaign_id, Event.kind == ChatGM.kind)
                .order_by(Event.seq)
            )
        )
        begun = list(
            db.exec(
                select(Event)
                .where(Event.campaign_id == campaign_id, Event.kind == "turn.begun")
                .order_by(Event.seq)
            )
        )
    assert json.loads(replies[-1].payload)["content"] == "The market stirs.\n\nHalta waits."
    assert "Game upgrade (out of character)" in json.dumps(json.loads(begun[-1].payload))


def test_a_campaign_with_no_recorded_version_can_be_upgraded(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign(client_factory)
    _set_version(client, campaign_id, "")

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "before the game recorded versions" in page

    assert _upgrade(client, campaign_id).status_code == 303
    state = open_campaign(client.app, campaign_id)
    assert state.upgrade is not None
    assert state.upgrade.from_version == ""
    assert state.upgrade.to_version == __version__
