from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, Event, User
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services import campaign_files
from cygnusloom.services import campaigns as campaigns_service
from cygnusloom.services.events import (
    EVENT_TYPES,
    ChatGM,
    ChatPlayer,
    NoteAdded,
    NoteRemoved,
    NoteUpdated,
    ObjectPlaced,
    ObjectTaken,
    StorylineCreated,
    StorylineFocused,
    StorylineResolved,
    StorylineUpdated,
    SummaryUpdated,
    record,
)
from tests.conftest import csrf_token, make_app
from tests.factories import (
    campaign_ready_fast,
    db,
    open_campaign,
    pending_turn,
    signed_in_client,
)


def _export(client: TestClient, campaign_id: int):
    response = client.get(f"/campaigns/{campaign_id}/export")
    assert response.status_code == 200, response.text
    return response


def _import(client: TestClient, payload: bytes, filename: str = "campaign.json"):
    token = csrf_token(client, "/campaigns")
    return client.post(
        "/campaigns/import",
        data={"csrf_token": token},
        files={"file": (filename, payload, "application/json")},
        follow_redirects=False,
    )


def _target_app(
    tmp_path, settings: Settings, models: tuple[str, ...] = ("fake/scripted",)
) -> FastAPI:
    target = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'target.db'}",
        llm_models=models,
        llm_default_model=models[0],
        auto_create_schema=True,
    )
    return make_app(target, FakeProvider())


def _target_client(app: FastAPI) -> TestClient:
    return signed_in_client(lambda: TestClient(app))


def _imported_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_the_export_declares_the_campaign_major(client_factory, tmp_path, settings):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = json.loads(_export(client, campaign_id).content)
    assert document["campaign"]["major_version"] == 4


def test_another_major_imports_but_cannot_be_played(client_factory, tmp_path, settings):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = json.loads(_export(client, campaign_id).content)
    document["campaign"]["major_version"] = 3
    payload = json.dumps(document).encode()

    target = _target_client(_target_app(tmp_path, settings))
    response = _import(target, payload)
    imported_id = _imported_id(response)

    page = target.get(f"/campaigns/{imported_id}")
    assert page.status_code == 200
    assert "cannot play it" in page.text
    assert (
        target.post(
            f"/campaigns/{imported_id}/messages",
            data={
                "csrf_token": csrf_token(target, f"/campaigns/{imported_id}"),
                "content": "Hello",
            },
            follow_redirects=False,
        ).status_code
        == 409
    )

    exported = json.loads(_export(target, imported_id).content)
    assert exported["campaign"]["major_version"] == 3


def _add_referenced_events(app: FastAPI, campaign_id: int, *, close: bool = True) -> None:
    with db(app) as session:
        note_id = record(
            session,
            campaign_id,
            NoteAdded(text="A key under the stone.", subject="The mill"),
        )
        record(
            session,
            campaign_id,
            NoteUpdated(note_id=note_id, text="A brass key under the stone."),
        )
        removed_id = record(session, campaign_id, NoteAdded(text="Scratch this.", subject=None))
        record(
            session,
            campaign_id,
            NoteRemoved(note_id=removed_id, text="Scratch this."),
        )
        heir_id = record(
            session,
            campaign_id,
            StorylineCreated(
                name="The Missing Heir",
                summary="The heir is missing.",
                plot="The witch took him.",
                characters=["Mara"],
                locations=["The mill"],
                objects=["A brass key"],
                notes="The key opens the cellar.",
            ),
        )
        record(
            session,
            campaign_id,
            StorylineFocused(storyline_id=heir_id, name="The Missing Heir"),
        )
        salt_id = record(
            session,
            campaign_id,
            StorylineCreated(name="The Salt Road", summary="The road is closed."),
        )
        record(
            session,
            campaign_id,
            StorylineUpdated(
                storyline_id=salt_id,
                name="The Salt Road",
                summary="The road is closed.",
                plot="",
                characters=[],
                locations=[],
                objects=[],
                notes="",
                active=False,
            ),
        )
        record(
            session,
            campaign_id,
            StorylineResolved(
                storyline_id=salt_id,
                name="The Salt Road",
                resolution="The road reopened before the harvest.",
            ),
        )
        object_id = record(
            session,
            campaign_id,
            ObjectPlaced(place="Mossbell Hollow", contents="A brass key."),
        )
        record(
            session,
            campaign_id,
            ObjectPlaced(place="The attic", contents="A dusty ledger."),
        )
        record(
            session,
            campaign_id,
            ObjectTaken(object_id=object_id, name="Ada", contents="A brass key."),
        )
        chat_id = record(
            session,
            campaign_id,
            ChatPlayer(content="I search the yard.", speaker="Ada"),
        )
        if close:
            record(session, campaign_id, ChatGM(content="The yard is quiet.", model="test"))
        record(
            session,
            campaign_id,
            SummaryUpdated(summary="Ada searched the yard.", through=chat_id),
        )
        session.commit()


def _events(app: FastAPI, campaign_id: int) -> list[Event]:
    with Session(app.state.engine) as session:
        return list(
            session.execute(
                select(Event).where(Event.campaign_id == campaign_id).order_by(Event.seq)
            ).scalars()
        )


def _by_kind(app: FastAPI, campaign_id: int) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for row in _events(app, campaign_id):
        grouped.setdefault(row.kind, []).append(row)
    return grouped


def test_export_and_import_round_trip(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    _add_referenced_events(client.app, campaign_id)
    response = _export(client, campaign_id)
    document = response.json()
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="cygnusloom-contract-hollow-'
    )
    assert document["format"] == "cygnusloom.campaign"
    assert document["version"] == 1
    assert document["campaign"]["world_template"] == "testhollow"
    assert "game_version" not in document["campaign"]
    assert document["source"]["event_count"] == len(document["events"])
    assert "owner_id" not in document["campaign"]
    assert "api_key" not in json.dumps(document)

    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    imported = _import(target_client, response.content)
    new_id = _imported_id(imported)

    source = open_campaign(client.app, campaign_id)
    target = open_campaign(target_app, new_id)
    assert set(source.characters) == set(target.characters)
    assert set(source.places) == set(target.places)
    assert {
        name: (character.location, character.name) for name, character in source.characters.items()
    } == {
        name: (character.location, character.name) for name, character in target.characters.items()
    }
    assert [(note.subject, note.text) for note in source.notes] == [
        (note.subject, note.text) for note in target.notes
    ]
    assert [
        (
            storyline.name,
            storyline.summary,
            storyline.plot,
            storyline.characters,
            storyline.locations,
            storyline.objects,
            storyline.notes,
            storyline.active,
            storyline.resolved,
            storyline.resolution,
        )
        for storyline in source.storylines.values()
    ] == [
        (
            storyline.name,
            storyline.summary,
            storyline.plot,
            storyline.characters,
            storyline.locations,
            storyline.objects,
            storyline.notes,
            storyline.active,
            storyline.resolved,
            storyline.resolution,
        )
        for storyline in target.storylines.values()
    ]
    assert (
        source.storylines[source.focus_storyline_id].name
        if source.focus_storyline_id is not None
        else None
    ) == (
        target.storylines[target.focus_storyline_id].name
        if target.focus_storyline_id is not None
        else None
    )
    assert sorted((obj.place, obj.contents, obj.item) for obj in source.objects.values()) == sorted(
        (obj.place, obj.contents, obj.item) for obj in target.objects.values()
    )
    assert set(source.maps) == set(target.maps)
    for name, encounter_map in source.maps.items():
        assert (encounter_map.rows, encounter_map.columns) == (
            target.maps[name].rows,
            target.maps[name].columns,
        )
    assert source.summary == target.summary
    assert source.journal == target.journal
    assert source.turn is None and target.turn is None
    target_chat = _by_kind(target_app, new_id)["chat.player"][0]
    assert target.through == target_chat.id

    with Session(target_app.state.engine) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None
        assert campaign.name == "Contract Hollow"
        assert campaign.world_template == "testhollow"
        assert campaign.rule_set == "rolltest"
        assert campaign.model == "fake/scripted"
        owner = session.get(User, campaign.owner_id)
        assert owner is not None
        assert owner.email == "ada@example.com"
        assert len(_events(target_app, new_id)) == len(document["events"])


def test_an_old_major_export_stores_its_events_opaque(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    """A removed event kind from another major is kept verbatim, but the log is not replayed."""
    client, campaign_id = campaign_ready_fast(client_factory)
    _add_referenced_events(client.app, campaign_id)
    document = _export(client, campaign_id).json()
    document["campaign"]["major_version"] = 3
    last = document["events"][-1]
    document["events"].append(
        {
            "seq": last["seq"] + 1,
            "id": last["id"] + 1,
            "kind": "campaign.upgraded",
            "created_at": last["created_at"],
            "payload": {"from_version": "3.0.0", "to_version": "3.6.0"},
        }
    )
    document["source"]["event_count"] = len(document["events"])
    document["source"]["last_event_id"] = last["id"] + 1

    target_app = _target_app(tmp_path, settings)
    new_id = _imported_id(_import(_target_client(target_app), json.dumps(document).encode()))

    with db(target_app) as session:
        campaign = session.get(Campaign, new_id)
        assert campaign is not None and campaign.major_version == 3
        kinds = list(session.exec(select(Event.kind).where(Event.campaign_id == new_id)).all())
    assert "campaign.upgraded" in kinds
    # A foreign-major import still remaps references between known event kinds, so a later
    # upgrade does not inherit ids from the source database.
    grouped = _by_kind(target_app, new_id)
    note_added = next(
        row for row in grouped["note.added"] if "A key under the stone." in row.payload
    )
    note_updated = grouped["note.updated"][0]
    assert json.loads(note_updated.payload)["note_id"] == note_added.id


def test_import_remaps_event_id_references(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    _add_referenced_events(client.app, campaign_id)
    document = _export(client, campaign_id).json()
    for event in document["events"]:
        event["id"] += 1000
        for field_name in EVENT_TYPES[event["kind"]].id_refs:
            if field_name in event["payload"]:
                event["payload"][field_name] += 1000
    document["source"]["last_event_id"] += 1000
    source_note_id = next(
        event["id"]
        for event in document["events"]
        if event["kind"] == "note.added"
        and event["payload"].get("text") == "A key under the stone."
    )

    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    new_id = _imported_id(_import(target_client, json.dumps(document).encode()))
    grouped = _by_kind(target_app, new_id)
    added_notes = [
        row
        for row in grouped["note.added"]
        if json.loads(row.payload)["text"] in {"A key under the stone.", "Scratch this."}
    ]
    kept = next(
        row for row in added_notes if json.loads(row.payload)["text"] == "A key under the stone."
    )
    removed = next(row for row in added_notes if json.loads(row.payload)["text"] == "Scratch this.")
    assert kept.id != source_note_id
    assert json.loads(grouped["note.updated"][0].payload)["note_id"] == kept.id
    assert json.loads(grouped["note.removed"][0].payload)["note_id"] == removed.id
    placed = next(
        row
        for row in grouped["object.placed"]
        if json.loads(row.payload)["contents"] == "A brass key."
    )
    assert json.loads(grouped["object.taken"][0].payload)["object_id"] == placed.id
    heir = next(
        row
        for row in grouped["storyline.created"]
        if json.loads(row.payload)["name"] == "The Missing Heir"
    )
    assert json.loads(grouped["storyline.focused"][0].payload)["storyline_id"] == heir.id
    salt = next(
        row
        for row in grouped["storyline.created"]
        if json.loads(row.payload)["name"] == "The Salt Road"
    )
    assert json.loads(grouped["storyline.updated"][0].payload)["storyline_id"] == salt.id
    assert json.loads(grouped["storyline.resolved"][0].payload)["storyline_id"] == salt.id
    chat = grouped["chat.player"][0]
    assert json.loads(grouped["summary.updated"][0].payload)["through"] == chat.id


def test_import_uses_the_default_when_the_model_is_missing(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    with db(client.app) as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = "ollama/test-model"
        session.commit()
    payload = _export(client, campaign_id).content
    target_app = _target_app(tmp_path, settings, models=("fake/scripted",))
    target_client = _target_client(target_app)
    response = _import(target_client, payload)
    new_id = _imported_id(response)
    with Session(target_app.state.engine) as session:
        imported = session.get(Campaign, new_id)
        assert imported is not None
        assert imported.model == "fake/scripted"
    page = target_client.get(response.headers["location"])
    assert "not on this server" in page.text


def test_round_trip_keeps_an_open_turn(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    _add_referenced_events(client.app, campaign_id, close=False)
    source_turn = pending_turn(client.app, campaign_id)
    assert source_turn is not None
    payload = _export(client, campaign_id).content
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    new_id = _imported_id(_import(target_client, payload))
    target_turn = pending_turn(target_app, new_id)
    assert target_turn is not None
    assert target_turn.text == source_turn.text
    assert target_turn.opening is False


def test_invalid_campaign_files_are_rejected(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _export(client, campaign_id).json()
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    cases = {
        "format": lambda value: value.update(format="something-else"),
        "version": lambda value: value.update(version=99),
        "kind": lambda value: value["events"][0].update(kind="not.a.kind"),
        "seq": lambda value: value["events"][1].update(seq=value["events"][0]["seq"]),
        "id": lambda value: value["events"][1].update(id=value["events"][0]["id"]),
        "world": lambda value: value["campaign"].update(world_template="missing-world"),
        "payload": lambda value: value["events"][0].update(payload=[]),
    }
    for name, mutate in cases.items():
        candidate = json.loads(json.dumps(document))
        mutate(candidate)
        response = _import(target_client, json.dumps(candidate).encode())
        assert response.status_code == 303, name
        page = target_client.get("/campaigns")
        assert "Could not import" in page.text, name
    with Session(target_app.state.engine) as session:
        assert session.execute(select(Campaign)).scalars().first() is None


def test_import_suffixes_a_duplicate_name(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    payload = _export(client, campaign_id).content
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    _imported_id(_import(target_client, payload))
    second = _imported_id(_import(target_client, payload))
    with Session(target_app.state.engine) as session:
        names = set(session.execute(select(Campaign.name)).scalars())
        imported = session.get(Campaign, second)
        assert imported is not None
        assert imported.name == "Contract Hollow (imported)"
    assert "Contract Hollow" in names


def test_import_requires_csrf(tmp_path, settings: Settings):
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    response = target_client.post(
        "/campaigns/import",
        files={"file": ("campaign.json", b"{}", "application/json")},
    )
    assert response.status_code == 403


def test_other_accounts_cannot_export(client_factory: Callable[[], TestClient]):
    _client, campaign_id = campaign_ready_fast(client_factory)
    intruder = signed_in_client(client_factory, "intruder@example.com")
    response = intruder.get(f"/campaigns/{campaign_id}/export")
    assert response.status_code == 404


def test_export_is_refused_while_the_gm_is_working(client_factory: Callable[[], TestClient]):
    client, campaign_id = campaign_ready_fast(client_factory)
    with campaigns_service._exclusive(campaign_id):
        response = client.get(f"/campaigns/{campaign_id}/export")
    assert response.status_code == 409


def test_campaign_pages_offer_import_and_export(client_factory: Callable[[], TestClient]):
    client, campaign_id = campaign_ready_fast(client_factory)
    page = client.get("/campaigns").text
    assert 'action="/campaigns/import"' in page
    assert 'enctype="multipart/form-data"' in page
    assert f'href="/campaigns/{campaign_id}/export"' in page
    campaign_page = client.get(f"/campaigns/{campaign_id}").text
    assert f'href="/campaigns/{campaign_id}/export"' in campaign_page


def test_import_rejects_a_file_over_the_limit(
    tmp_path, settings: Settings, client_factory: Callable[[], TestClient], monkeypatch
):
    client, campaign_id = campaign_ready_fast(client_factory)
    payload = _export(client, campaign_id).content
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    monkeypatch.setattr(campaign_files, "MAX_IMPORT_BYTES", 10)
    response = _import(target_client, payload)
    assert response.status_code == 303
    page = target_client.get("/campaigns")
    assert "larger than" in page.text
