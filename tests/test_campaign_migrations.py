from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from cygnusloom import CAMPAIGN_MAJOR_VERSION, campaign_migrations
from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, Event
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.events import NoteAdded, NoteUpdated, is_hidden, record
from tests.conftest import csrf_token, make_app
from tests.factories import (
    WWN_STATS,
    campaign_ready_fast,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    db,
    pinned_stats,
    signed_in_client,
)
from tests.test_character_creation import _choices, _named_picks, _picks, _rolled_stats


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
    assert "needs a one-time upgrade" in blocked.text
    assert "cannot play it" not in blocked.text
    assert "This campaign was made with Cygnus Loom 3." in blocked.text
    assert f"Upgrade to Cygnus Loom {CAMPAIGN_MAJOR_VERSION}" in blocked.text
    listing = target.get("/campaigns")
    assert "needs attention" in listing.text
    assert "not playable" not in listing.text

    response = _convert(target, new_id)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/campaigns/{new_id}"

    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert "needs a one-time upgrade" not in page.text
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


def test_the_blocked_page_hides_the_upgrade_when_no_migration_chain_exists(
    tmp_path, settings: Settings, client_factory, monkeypatch
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _old_document(client, campaign_id)
    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())

    monkeypatch.setattr(campaign_migrations, "supports", lambda major: False)
    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200
    assert f"Upgrade to Cygnus Loom {CAMPAIGN_MAJOR_VERSION}" not in page.text
    assert "cannot upgrade this campaign" in page.text


def _v4_document(client: TestClient, campaign_id: int) -> dict[str, Any]:
    """A current export relabelled as a 4.x file."""
    document = json.loads(_export(client, campaign_id).content)
    document["campaign"]["major_version"] = 4
    return document


def _append_events(document: dict[str, Any], additions: Iterable[dict[str, Any]]) -> None:
    last = document["events"][-1]
    for index, event in enumerate(additions, start=1):
        document["events"].append(
            {
                "seq": last["seq"] + index,
                "id": last["id"] + index,
                "created_at": last["created_at"],
                **event,
            }
        )
    document["source"]["event_count"] = len(document["events"])
    document["source"]["last_event_id"] = last["id"] + len(additions)


def _wwn_campaign(client_factory) -> tuple[TestClient, int]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="worldswithoutnumber"
    )
    with pinned_stats(WWN_STATS):
        page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(_picks(page))
    fields.update(
        {
            "template-background": "artisan",
            "template-class": "expert",
            "template-tradition": "no_tradition",
            "pick-skill-artisan-background_learning-0": "craft",
            "pick-skill-artisan-background_learning-1": "notice",
            "pick-skill-expert-class_free_skill-0": "know",
            "pick-feature-expert-class_focus_any-0": "focus_artisan",
            "pick-feature-expert-class_focus_expert-0": "focus_connected",
            "skill-replacement-feature": "perform",
        }
    )
    create_character_direct(client, campaign_id, fields=_named_picks(fields))
    close_opening(client, campaign_id)
    return client, campaign_id


def _dnd_campaign(client_factory) -> tuple[TestClient, int]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(
        client,
        campaign_id,
        name="Rook",
        fields={
            "stat-str": "15",
            "stat-dex": "14",
            "stat-con": "13",
            "stat-int": "12",
            "stat-wis": "10",
            "stat-cha": "8",
            "campaign-xp_rate": "Standard",
            "template-species": "human",
            "template-background": "soldier",
            "template-class": "fighter",
            "pick-skill-class_skills-0": "history",
            "pick-skill-class_skills-1": "perception",
            "pick-feature-fighting_style-0": "feat-defense",
            "feature-choice-background-asi-soldier-scores-0": "str",
            "feature-choice-background-asi-soldier-scores-1": "dex",
            "feature-choice-background-asi-soldier-scores-2": "con",
        },
    )
    close_opening(client, campaign_id)
    return client, campaign_id


def test_the_v4_migration_restores_items_currency_and_newer_event_fields(
    tmp_path, settings: Settings, client_factory
):
    client, campaign_id = _wwn_campaign(client_factory)
    document = _v4_document(client, campaign_id)
    events = document["events"]
    places = [event for event in events if event["kind"] == "location.added"]
    origin, destination = places[0], places[1]
    origin["payload"]["location_id"] = None
    origin["payload"]["routes"] = {
        destination["payload"]["name"]: {
            "path": [],
            "distance": 1,
            "terrain": "plain",
            "vast": False,
        }
    }
    pc = next(event for event in events if event["kind"] == "pc.added")
    pc["payload"]["features"]["daily-ration"] = 1
    rations = next(
        event
        for event in events
        if event["kind"] == "character.item_added"
        and event["payload"]["item"] == "Rations, One Week"
    )
    rations["payload"]["uses"] = 0
    rations["payload"]["max_uses"] = 0
    depleted = next(
        event
        for event in events
        if event["kind"] == "character.item_added" and event["payload"]["item"] == "Torch"
    )
    depleted["payload"]["uses"] = 0
    depleted["payload"]["max_uses"] = 1
    _append_events(
        document,
        [
            {
                "kind": "character.currency_changed",
                "channel": "log",
                "payload": {
                    "name": "Ada",
                    "feature": "silver",
                    "amount": 12,
                    "cause": "a test purse",
                },
            },
            {
                "kind": "character.debug_currency_added",
                "channel": "internal",
                "payload": {"name": "Ada", "feature": "silver", "amount": 3},
            },
            {
                "kind": "character.item_uses_set",
                "channel": "log",
                "payload": {
                    "name": "Ada",
                    "item": "Rations, One Week",
                    "uses": 6,
                    "max_uses": 7,
                    "cause": "used",
                },
            },
            {
                "kind": "actor.moved",
                "channel": "log",
                "payload": {"name": "Ada", "location": origin["payload"]["name"]},
            },
            {
                "kind": "object.placed",
                "channel": "log",
                "payload": {
                    "place": origin["payload"]["name"],
                    "contents": "A brass key.",
                    "item": "brass key",
                    "item_size": "normal",
                    "item_slots": 1,
                },
            },
        ],
    )

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    assert _convert(target, new_id).status_code == 303
    page = target.get(f"/campaigns/{new_id}")
    assert page.status_code == 200, page.text
    assert "needs a one-time upgrade" not in page.text

    rows = _events(target.app, new_id)
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(json.loads(row.payload))
    assert all("feature" not in payload for payload in by_kind["character.currency_changed"])
    assert [payload["item"] for payload in by_kind["character.currency_changed"]] == ["silver"]
    assert [payload["item"] for payload in by_kind["character.debug_currency_added"]] == ["silver"]
    pc_after = by_kind["pc.added"][0]
    assert "daily-ration" not in pc_after["features"]
    rations_after = next(
        payload
        for payload in by_kind["character.item_added"]
        if payload["item"] == "Rations, One Week"
    )
    assert (rations_after["uses"], rations_after["max_uses"]) == (7, 7)
    torches = [payload for payload in by_kind["character.item_added"] if payload["item"] == "Torch"]
    assert (0, 1) in [(payload["uses"], payload["max_uses"]) for payload in torches]
    assert "max_uses" not in by_kind["character.item_uses_set"][0]
    assert by_kind["actor.moved"][0]["quiet"] is False
    assert by_kind["object.placed"][0]["item_text"] == ""
    origin_after = next(
        payload
        for payload in by_kind["location.added"]
        if payload["name"] == origin["payload"]["name"]
    )
    destination_after = next(
        payload
        for payload in by_kind["location.added"]
        if payload["name"] == destination["payload"]["name"]
    )
    assert origin_after["location_id"]
    assert list(origin_after["routes"]) == [destination_after["location_id"]]

    from tests.factories import character_named

    ada = character_named(target.app, new_id, "Ada")
    assert ada.currency["silver"] == 15
    assert "daily-ration" not in ada.features
    rations_held = next(item for item in ada.items if item.name == "Rations, One Week")
    assert (rations_held.uses, rations_held.max_uses) == (6, 7)


def test_the_v4_migration_turns_an_item_feature_into_a_feature_choice(
    tmp_path, settings: Settings, client_factory
):
    client, campaign_id = _dnd_campaign(client_factory)
    document = _v4_document(client, campaign_id)
    events = document["events"]
    pc = next(event for event in events if event["kind"] == "pc.added")
    pc["payload"]["features"]["weapon-longsword"] = 1
    currency = next(event for event in events if event["kind"] == "character.currency_changed")
    currency["payload"]["feature"] = currency["payload"].pop("item")
    _append_events(
        document,
        [
            {
                "kind": "character.feature_learned",
                "channel": "log",
                "payload": {
                    "name": "Rook",
                    "feature": "weapon-longsword",
                    "feature_name": "Longsword",
                    "cause": "Weapon Mastery",
                },
            }
        ],
    )

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    assert _convert(target, new_id).status_code == 303
    assert target.get(f"/campaigns/{new_id}").status_code == 200

    rows = _events(target.app, new_id)
    payloads = [
        json.loads(row.payload) for row in rows if row.kind == "character.feature_choice_made"
    ]
    assert len(payloads) == 1
    assert payloads[0] == {
        "name": "Rook",
        "feature": "class-fighter-weapon-mastery",
        "pick": "weapons",
        "choice": "weapon-longsword",
    }
    assert not any(
        row.kind == "character.feature_learned"
        and json.loads(row.payload).get("feature") == "weapon-longsword"
        for row in rows
    )
    pc_after = next(json.loads(row.payload) for row in rows if row.kind == "pc.added")
    assert "weapon-longsword" not in pc_after["features"]
    currency_after = next(
        json.loads(row.payload) for row in rows if row.kind == "character.currency_changed"
    )
    assert currency_after["item"] == "silver"
    assert "feature" not in currency_after

    from tests.factories import character_named

    rook = character_named(target.app, new_id, "Rook")
    assert rook.feature_choices["class-fighter-weapon-mastery:weapons"] == ["weapon-longsword"]
    assert "weapon-longsword" not in rook.features


def test_conversion_starts_a_waiting_player_turn(tmp_path, settings: Settings, client_factory):
    from tests.test_encounters import _call, _encounter, _encounter_with

    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    _call(client.app, campaign_id, "remove_participant", {"name": "Pip"})
    assert _encounter(client.app, campaign_id).encounter.acting is None
    document = _v4_document(client, campaign_id)

    target = _target_client(_target_app(tmp_path, settings))
    new_id = _import(target, json.dumps(document).encode())
    assert _convert(target, new_id).status_code == 303

    assert _encounter(target.app, new_id).encounter.acting == "Ada"
    kinds = [row.kind for row in _events(target.app, new_id)]
    assert kinds[-2:] == ["encounter.turn_begun", "campaign.converted"]
