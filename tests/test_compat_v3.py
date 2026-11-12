from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from cygnusloom import CAMPAIGN_MAJOR_VERSION
from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, Event
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.events import NoteAdded, NoteUpdated, is_hidden, record
from tests.conftest import csrf_token, make_app
from tests.factories import campaign_ready_fast, db, signed_in_client


def _export(client: TestClient, campaign_id: int):
    response = client.get(f"/campaigns/{campaign_id}/export")
    assert response.status_code == 200, response.text
    return response


def _import(client: TestClient, payload: bytes) -> int:
    response = client.post(
        "/campaigns/import",
        data={"csrf_token": csrf_token(client, "/campaigns")},
        files={"file": ("campaign.json", payload, "application/json")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _target_app(tmp_path, settings: Settings) -> FastAPI:
    target = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'target.db'}",
        llm_models=("fake/scripted",),
        llm_default_model="fake/scripted",
        auto_create_schema=True,
    )
    return make_app(target, FakeProvider())


def _target_client(app: FastAPI) -> TestClient:
    return signed_in_client(lambda: TestClient(app))


def _old_document(
    client: TestClient,
    campaign_id: int,
    *,
    close_upgrade: bool = True,
    extra: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """A current export relabelled as a 3.x file, with the upgrade turn a 3.x log records."""
    document = json.loads(_export(client, campaign_id).content)
    document["campaign"]["major_version"] = 3
    last = document["events"][-1]
    created_at = last["created_at"]
    additions = [
        {
            "kind": "campaign.upgraded",
            "channel": "log",
            "payload": {"from_version": "3.4.0", "to_version": "3.6.0"},
        },
        {
            "kind": "turn.begun",
            "channel": "internal",
            "payload": {
                "head": [
                    {
                        "role": "user",
                        "source": "Game upgrade notice",
                        "content": "Acknowledge the upgrade out of character.",
                    }
                ],
                "context": [],
                "messages": [],
                "round_start": 0,
            },
        },
        *(
            [
                {
                    "kind": "chat.gm",
                    "channel": "transcript",
                    "payload": {
                        "content": "Out of character: this campaign has been upgraded.",
                        "model": "test",
                        "final": True,
                    },
                }
            ]
            if close_upgrade
            else []
        ),
        *extra,
    ]
    for index, event in enumerate(additions, start=1):
        document["events"].append(
            {
                "seq": last["seq"] + index,
                "id": last["id"] + index,
                "created_at": created_at,
                **event,
            }
        )
    document["source"]["event_count"] = len(document["events"])
    document["source"]["last_event_id"] = last["id"] + len(additions)
    return document


def _add_note_pair(client: TestClient, campaign_id: int) -> None:
    with db(client.app) as session:
        note_id = record(
            session,
            campaign_id,
            NoteAdded(text="A key under the stone.", subject="The mill"),
        )
        session.commit()
        record(
            session,
            campaign_id,
            NoteUpdated(note_id=note_id, text="A brass key under the stone."),
        )
        session.commit()


def _convert(client: TestClient, campaign_id: int):
    return client.post(
        f"/campaigns/{campaign_id}/convert",
        data={"csrf_token": csrf_token(client, f"/campaigns/{campaign_id}")},
        follow_redirects=False,
    )


def _events(app: FastAPI, campaign_id: int) -> list[Event]:
    with db(app) as session:
        return list(
            session.exec(select(Event).where(Event.campaign_id == campaign_id).order_by(Event.seq))
        )


def test_a_3x_campaign_converts_in_place_and_keeps_its_log(
    tmp_path, settings: Settings, client_factory
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(client, campaign_id)

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    blocked = target.get(f"/campaigns/{new_id}")
    assert blocked.status_code == 200
    assert "cannot play it" in blocked.text
    assert "This campaign was made for Cygnus Loom 3." in blocked.text
    assert f"Upgrade to Cygnus Loom {CAMPAIGN_MAJOR_VERSION}" in blocked.text
    listing = target.get("/campaigns")
    assert "needs attention" in listing.text
    assert "not playable" not in listing.text

    response = _convert(target, new_id)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/campaigns/{new_id}"

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "cannot play it" not in page.text
    assert (
        f"This campaign was upgraded from Cygnus Loom 3 to Cygnus Loom {CAMPAIGN_MAJOR_VERSION}."
    ) in page.text
    assert "The rules and tools may have changed; the story continues" in page.text
    assert not is_hidden("campaign.converted")

    with db(target.app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == CAMPAIGN_MAJOR_VERSION
        rows = _events(target.app, new_id)
    kinds = [row.kind for row in rows]
    assert "campaign.upgraded" not in kinds
    assert "campaign.converted" in kinds
    assert kinds.count("turn.opening") >= 2
    converted = next(row for row in rows if row.kind == "campaign.converted")
    payload = json.loads(converted.payload)
    assert payload["from_major"] == 3
    assert payload["to_major"] == CAMPAIGN_MAJOR_VERSION
    assert payload["from_version"] == "3.6.0"
    assert "Out of character" in page.text


def test_an_upgraded_campaign_round_trips_through_export(
    tmp_path, settings: Settings, client_factory
):
    client, campaign_id = campaign_ready_fast(client_factory)
    _add_note_pair(client, campaign_id)
    document = _old_document(client, campaign_id)

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    assert _convert(target, new_id).status_code == 303

    exported = target.get(f"/campaigns/{new_id}/export")
    assert exported.status_code == 200, exported.text
    imported = target.post(
        "/campaigns/import",
        data={"csrf_token": csrf_token(target, "/campaigns")},
        files={"file": ("campaign.json", exported.content, "application/json")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    assert imported.headers["location"] != "/campaigns", imported.text
    copy_id = int(imported.headers["location"].rsplit("/", 1)[-1])
    page = target.get(f"/campaigns/{copy_id}")
    assert page.status_code == 200, page.text
    with db(target.app) as session:
        copy = session.get(Campaign, copy_id)
        assert copy is not None and copy.major_version == CAMPAIGN_MAJOR_VERSION


def test_an_unknown_event_kind_leaves_the_campaign_untouched(
    tmp_path, settings: Settings, client_factory
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(
        client,
        campaign_id,
        extra=[
            {
                "kind": "legacy.unknown",
                "channel": "log",
                "payload": {"note": "kept only by a 3.x engine"},
            }
        ],
    )

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    response = _convert(target, new_id)
    assert response.status_code == 303

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "event kind" in page.text and "legacy.unknown" in page.text

    with db(target.app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == 3
        kinds = [row.kind for row in _events(target.app, new_id)]
    assert "campaign.upgraded" in kinds
    assert "legacy.unknown" in kinds


def test_a_log_that_cannot_be_replayed_is_not_stamped(tmp_path, settings: Settings, client_factory):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(
        client,
        campaign_id,
        extra=[
            {
                "kind": "turn.begun",
                "channel": "internal",
                "payload": {"head": [], "context": [], "messages": [], "round_start": 0},
            }
        ],
    )

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    response = _convert(target, new_id)
    assert response.status_code == 303

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "could not be replayed" in page.text

    with db(target.app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == 3
        kinds = [row.kind for row in _events(target.app, new_id)]
    assert "campaign.upgraded" in kinds
    assert "campaign.converted" not in kinds


def test_an_open_turn_blocks_conversion(tmp_path, settings: Settings, client_factory):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(client, campaign_id, close_upgrade=False)

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    response = _convert(target, new_id)
    assert response.status_code == 303

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "open turn" in page.text

    with db(target.app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == 3
        kinds = [row.kind for row in _events(target.app, new_id)]
    assert "campaign.converted" not in kinds


def test_missing_content_blocks_conversion(tmp_path, settings: Settings, client_factory):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(client, campaign_id)
    pc = next(event for event in document["events"] if event["kind"] == "pc.added")
    pc["payload"]["features"]["missing-feature"] = 1

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    response = _convert(target, new_id)
    assert response.status_code == 303

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "missing-feature" in page.text

    with db(target.app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == 3
        kinds = [row.kind for row in _events(target.app, new_id)]
    assert "campaign.upgraded" in kinds
    assert "campaign.converted" not in kinds


def test_missing_condition_blocks_conversion(tmp_path, settings: Settings, client_factory):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(
        client,
        campaign_id,
        extra=[
            {
                "kind": "condition.added",
                "channel": "log",
                "payload": {"name": "Ada", "condition": "missing-condition"},
            }
        ],
    )

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    assert _convert(target, new_id).status_code == 303

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "missing-condition" in page.text

    with db(target.app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == 3


def test_a_converted_campaign_starts_a_turn(tmp_path, settings: Settings, client_factory):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(client, campaign_id)

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    assert _convert(target, new_id).status_code == 303

    response = target.post(
        f"/campaigns/{new_id}/messages",
        data={
            "csrf_token": csrf_token(target, f"/campaigns/{new_id}"),
            "content": '__FAKE__:{"turns": [{"text": "The road waits."}]}',
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    replies = [row.payload for row in _events(target.app, new_id) if row.kind == "chat.gm"]
    assert any("The road waits." in json.loads(payload)["content"] for payload in replies)


def test_a_current_campaign_is_refused(tmp_path, settings: Settings, client_factory):
    client, campaign_id = campaign_ready_fast(client_factory)
    response = _convert(client, campaign_id)
    assert response.status_code == 303
    page = client.get(f"/campaigns/{campaign_id}")
    assert f"already plays on Cygnus Loom {CAMPAIGN_MAJOR_VERSION}" in page.text


def test_the_blocked_page_hides_the_upgrade_when_the_adapter_targets_another_major(
    tmp_path, settings: Settings, client_factory, monkeypatch
):
    from cygnusloom.services import compat_v3

    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(client, campaign_id)
    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())

    monkeypatch.setattr(compat_v3, "TO_MAJOR", CAMPAIGN_MAJOR_VERSION + 1)
    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert f"Upgrade to Cygnus Loom {CAMPAIGN_MAJOR_VERSION}" not in page.text
    assert "has no upgrade path" in page.text
