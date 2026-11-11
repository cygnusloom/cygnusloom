from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from cygnusloom.config import DEFAULT_MODEL_CONTEXT_TOKENS
from cygnusloom.db.models import CampaignInvite, Event, User
from cygnusloom.services import campaigns as campaigns_service
from cygnusloom.services import encounters
from cygnusloom.services import turns as turns_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import ChatGM, ChatPlayer, load_state, name_key, record
from cygnusloom.services.membership import (
    create_invite,
    is_member,
    list_invites,
    list_members,
    merge_debug_member,
)
from cygnusloom.web import campaigns as campaigns_web
from tests.conftest import csrf_token
from tests.factories import (
    create_campaign,
    create_character,
    db,
    open_campaign,
    place_pc,
    send_message,
    signed_in_client,
)

OWNER = "owner@example.com"
GUEST = "guest@example.com"


def _close_opening(client: TestClient, campaign_id: int) -> None:
    with db(client.app) as session:
        record(
            session,
            campaign_id,
            ChatGM(content="The opening plays out.", model="test"),
        )
        session.commit()


def _account(app, email: str) -> User:
    with db(app) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        assert user is not None, email
        return user


def _joined_campaign(
    client_factory: Callable[[], TestClient],
) -> tuple[TestClient, TestClient, int, CampaignInvite]:
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    created = create_character(owner, campaign_id, name="Ada")
    assert created.status_code == 303, created.text
    _close_opening(owner, campaign_id)
    invite = create_invite(owner.app.state.engine, _account(owner.app, OWNER).id, campaign_id)
    guest = signed_in_client(client_factory, GUEST)
    token = csrf_token(guest, f"/join/{invite.code}")
    joined = guest.post(
        f"/join/{invite.code}",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert joined.status_code == 303, joined.text
    assert joined.headers["location"] == f"/campaigns/{campaign_id}"
    created = create_character(guest, campaign_id, name="Brenna")
    assert created.status_code == 303, created.text
    return owner, guest, campaign_id, invite


def test_a_share_link_joins_the_campaign_and_lists_it_for_the_player(
    client_factory: Callable[[], TestClient],
):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    created = create_character(owner, campaign_id, name="Ada")
    assert created.status_code == 303
    _close_opening(owner, campaign_id)

    token = csrf_token(owner, "/campaigns")
    shared = owner.post(
        f"/campaigns/{campaign_id}/share",
        data={"csrf_token": token, "label": "Friends"},
        follow_redirects=False,
    )
    assert shared.status_code == 303
    invites = list_invites(owner.app.state.engine, campaign_id)
    assert len(invites) == 1 and invites[0].label == "Friends"

    guest = signed_in_client(client_factory, GUEST)
    page = guest.get(f"/join/{invites[0].code}")
    assert page.status_code == 200
    assert "Join" in page.text and "owner" in page.text.lower()

    joined = guest.post(
        f"/join/{invites[0].code}",
        data={"csrf_token": csrf_token(guest, f"/join/{invites[0].code}")},
        follow_redirects=False,
    )
    assert joined.status_code == 303
    list_page = guest.get("/campaigns")
    assert "Shared by" in list_page.text
    assert OWNER.split("@")[0] in list_page.text
    assert OWNER not in list_page.text
    assert GUEST not in list_page.text

    members = list_members(owner.app.state.engine, campaign_id)
    assert [m.display_name for m in members] == [OWNER.split("@")[0], GUEST.split("@")[0]]


def test_character_events_store_the_account_id_not_a_display_name(
    client_factory: Callable[[], TestClient],
):
    owner, _guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    with db(app) as session:
        rows = session.exec(
            select(Event).where(Event.campaign_id == campaign_id, Event.kind == "pc.added")
        ).all()
    payloads = [json.loads(row.payload) for row in rows]
    assert payloads
    assert all("player_name" not in payload for payload in payloads)
    assert {payload["player_id"] for payload in payloads} == {
        _account(app, OWNER).id,
        _account(app, GUEST).id,
    }


def test_a_display_name_change_reaches_old_messages_and_the_gm(
    client_factory: Callable[[], TestClient], fake
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    guest_id = _account(app, GUEST).id

    script = json.dumps({"turns": [{"text": "Brenna studies the room."}]})
    assert send_message(guest, campaign_id, f"__FAKE__:{script}").status_code == 303

    with db(app) as session:
        user = session.get(User, guest_id)
        assert user is not None
        user.display_name = "Ashen Fox 4821"
        session.add(user)
        session.commit()

    # The transcript resolves the name from the account id, so the old message shows the new
    # name and never the account's email.
    page = owner.get(f"/campaigns/{campaign_id}").text
    assert "Ashen Fox 4821" in page
    assert GUEST not in page

    script = json.dumps({"turns": [{"text": "Ada waits."}]})
    assert send_message(owner, campaign_id, f"__FAKE__:{script}").status_code == 303
    flat = json.dumps(fake.calls[-1])
    assert "Ashen Fox 4821" in flat
    assert GUEST not in flat


def test_members_cannot_use_owner_only_controls(client_factory: Callable[[], TestClient]):
    _, guest, campaign_id, _ = _joined_campaign(client_factory)

    assert guest.get(f"/campaigns/{campaign_id}/export").status_code == 404
    token = csrf_token(guest, "/campaigns")
    assert (
        guest.post(
            f"/campaigns/{campaign_id}/label",
            data={"csrf_token": token, "label": "Mine now"},
            follow_redirects=False,
        ).status_code
        == 404
    )
    assert (
        guest.post(
            f"/campaigns/{campaign_id}/model",
            data={"csrf_token": token, "model": "fake/scripted"},
            follow_redirects=False,
        ).status_code
        == 404
    )
    assert (
        guest.post(
            f"/campaigns/{campaign_id}/delete",
            data={"csrf_token": token, "confirm": "anything"},
            follow_redirects=False,
        ).status_code
        == 404
    )


def test_deactivating_a_link_keeps_existing_members_but_refuses_new_ones(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, invite = _joined_campaign(client_factory)
    token = csrf_token(owner, "/campaigns")
    response = owner.post(
        f"/campaigns/{campaign_id}/share/{invite.id}/deactivate",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert guest.get(f"/campaigns/{campaign_id}").status_code == 200
    assert is_member(owner.app.state.engine, _account(owner.app, GUEST).id, campaign_id)

    stranger = signed_in_client(client_factory, "stranger@example.com")
    assert stranger.get(f"/join/{invite.code}").status_code == 404
    assert (
        stranger.post(
            f"/join/{invite.code}",
            data={"csrf_token": csrf_token(stranger, "/login")},
            follow_redirects=False,
        ).status_code
        == 404
    )


def test_each_player_creates_their_own_character_and_any_player_advances_the_story(
    client_factory: Callable[[], TestClient], fake
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    state = open_campaign(app, campaign_id)
    # A player joining a running campaign does not start a second opening turn.
    assert state.turn is None
    assert state.player_character(_account(app, GUEST).id).name == "Brenna"

    # The second player's message speaks for their own character.
    script = json.dumps({"turns": [{"text": "Brenna studies the room."}]})
    response = send_message(guest, campaign_id, f"__FAKE__:{script}")
    assert response.status_code == 303, response.text
    state = open_campaign(app, campaign_id)
    assert {c.name for c in state.characters.values()} >= {"Ada", "Brenna"}
    assert state.turn is None

    with db(app) as session:
        rows = session.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == ChatPlayer.kind)
            .order_by(Event.seq.desc())
        ).all()
    latest = json.loads(rows[0].payload)
    assert latest["speaker"] == "Brenna"
    assert latest["player_id"] == _account(app, GUEST).id
    # The log stores the account id, not a display name that could go stale or leak an email.
    assert "player_name" not in latest

    # The GM's prompt names every player and attributes the message to its account.
    flat = json.dumps(fake.calls[-1])
    assert "Several players share this campaign" in flat
    assert "Brenna (guest)" in flat
    assert GUEST not in flat

    # The owner can then move the story forward too, in the same campaign.
    script = json.dumps({"turns": [{"text": "Ada follows her in."}]})
    assert send_message(owner, campaign_id, f"__FAKE__:{script}").status_code == 303


def test_a_multiplayer_prompt_names_players_and_characters(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    script = json.dumps(
        {
            "turns": [
                {
                    "rounds": [
                        [
                            {
                                "name": "social_roll",
                                "arguments": {
                                    "prompt": "Roll to persuade the guard.",
                                    "relationship": 0,
                                    "circumstance": 0,
                                },
                            }
                        ]
                    ],
                    "text": "The guard waits.",
                }
            ]
        }
    )
    assert send_message(guest, campaign_id, f"__FAKE__:{script}").status_code == 303
    state = open_campaign(app, campaign_id)
    assert state.turn is not None and state.turn.prompt is not None
    assert state.turn.player_id == _account(app, GUEST).id

    status = guest.get(f"/campaigns/{campaign_id}/turn-status").json()
    assert status["status"] == "prompt"
    assert status["pending_for"] == _account(app, GUEST).id
    assert status["actor"]

    # Only the player whose message opened the reply answers its prompt.
    async def answer_as_owner():
        async for _ in turns_service.resume_turn(
            app.state.engine,
            app.state.providers,
            app.state.catalog.model_ids(),
            app.state.content,
            DEFAULT_MODEL_CONTEXT_TOKENS,
            _account(app, OWNER).id,
            campaign_id,
            "roll",
        ):
            pass

    with pytest.raises(InvalidInput, match="must answer"):
        asyncio.run(answer_as_owner())


def test_owner_may_keep_a_removed_players_character_or_retire_it(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    owner_id = _account(app, OWNER).id
    guest_id = _account(app, GUEST).id
    token = csrf_token(owner, "/campaigns")

    removed = owner.post(
        f"/campaigns/{campaign_id}/members/{guest_id}/remove",
        data={"csrf_token": token, "character": "retire"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert guest.get(f"/campaigns/{campaign_id}").status_code == 404
    brenda = open_campaign(app, campaign_id).characters[name_key("Brenna")]
    assert brenda.retired

    panel = owner.get(f"/campaigns/{campaign_id}")
    assert "Removed players" in panel.text and "Invite back" in panel.text

    # An active code does not undo a removal.
    fresh = create_invite(app.state.engine, owner_id, campaign_id)
    assert guest.get(f"/join/{fresh.code}").status_code == 403
    assert (
        guest.post(
            f"/join/{fresh.code}",
            data={"csrf_token": csrf_token(guest, "/campaigns")},
            follow_redirects=False,
        ).status_code
        == 403
    )
    # The owner can invite the same account back from the Players panel.
    readded = owner.post(
        f"/campaigns/{campaign_id}/members/{guest_id}/readd",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert readded.status_code == 303
    assert is_member(app.state.engine, guest_id, campaign_id)
    owner.post(
        f"/campaigns/{campaign_id}/members/{guest_id}/remove",
        data={"csrf_token": token, "character": "retire"},
        follow_redirects=False,
    )

    # A second guest is kept as an unclaimed part the table shares.
    second = signed_in_client(client_factory, "second@example.com")
    invite = create_invite(app.state.engine, owner_id, campaign_id)
    joined = second.post(
        f"/join/{invite.code}",
        data={"csrf_token": csrf_token(second, f"/join/{invite.code}")},
        follow_redirects=False,
    )
    assert joined.status_code == 303
    created = create_character(second, campaign_id, name="Cora")
    assert created.status_code == 303
    second_id = _account(app, "second@example.com").id
    for form_values in ({"character": "keep"},):
        removed = owner.post(
            f"/campaigns/{campaign_id}/members/{second_id}/remove",
            data={"csrf_token": token, **form_values},
            follow_redirects=False,
        )
        assert removed.status_code == 303
    cora = open_campaign(app, campaign_id).characters[name_key("Cora")]
    assert cora.unclaimed and not cora.out_of_play


def test_an_encounter_turn_belongs_to_the_characters_owner(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    place_pc(owner, campaign_id, "Mossbell Hollow", name="Ada")

    from tests.test_encounters import _call

    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {
            "name": "Hollow Yard",
            "location": "Mossbell Hollow",
            "columns": 6,
            "rows": 6,
            "description": "Open ground.",
        },
    )
    assert _call(app, campaign_id, "start_encounter", {"map": "Hollow Yard"})["started"] == (
        "encounter"
    )
    assert _call(app, campaign_id, "add_participant", {"name": "Ada"})["added"] == "Ada"
    assert _call(app, campaign_id, "add_participant", {"name": "Brenna"})["added"] == "Brenna"

    with db(app) as session:
        encounters.begin_turn(session, campaign_id, "Ada")
        session.commit()
    token = csrf_token(guest, "/campaigns")
    refused = guest.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": "Brenna acts out of turn."},
        follow_redirects=False,
    )
    assert refused.status_code == 400

    owner_token = csrf_token(owner, "/campaigns")
    allowed = owner.post(
        f"/campaigns/{campaign_id}/messages",
        data={
            "csrf_token": owner_token,
            "content": '__FAKE__:{"turns": [{"text": "Ada strikes first."}]}',
        },
        follow_redirects=False,
    )
    assert allowed.status_code == 303

    with db(app) as session:
        state = load_state(session, campaign_id)
        assert state.encounter is not None
        assert state.encounter.acting is not None
    # The guest cannot end another player's turn, but the owner can.
    denied = guest.post(
        f"/campaigns/{campaign_id}/encounter/turn/end",
        data={"csrf_token": token, "name": "Ada"},
        follow_redirects=False,
    )
    assert denied.status_code == 400
    ended = owner.post(
        f"/campaigns/{campaign_id}/encounter/turn/end",
        data={"csrf_token": owner_token, "name": "Ada"},
        follow_redirects=False,
    )
    assert ended.status_code == 303
    with db(app) as session:
        state = load_state(session, campaign_id)
        assert state.encounter is not None
        assert state.encounter.acting == "Brenna"


def test_debug_panel_can_add_a_player_without_a_join_link(client_factory: Callable[[], TestClient]):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    created = create_character(owner, campaign_id, name="Ada")
    assert created.status_code == 303
    owner_id = _account(owner.app, OWNER).id
    user = merge_debug_member(owner.app.state.engine, owner_id, campaign_id, "debug@example.com")
    assert user.id is not None
    assert is_member(owner.app.state.engine, user.id, campaign_id)


def test_debug_members_route_needs_the_debug_panel(client_factory: Callable[[], TestClient]):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    token = csrf_token(owner, "/campaigns")
    response = owner.post(
        f"/campaigns/{campaign_id}/debug/member",
        data={"csrf_token": token, "email": "debug@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_member_can_open_the_shared_campaign_and_its_live_views(
    client_factory: Callable[[], TestClient],
):
    _owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    page = guest.get(f"/campaigns/{campaign_id}")
    assert page.status_code == 200
    assert "Brenna" in page.text
    assert "Players" in page.text
    live = guest.get(f"/campaigns/{campaign_id}/live")
    assert live.status_code == 200
    assert "Brenna" in live.text
    assert guest.get(f"/campaigns/{campaign_id}/map").status_code == 200


def test_the_same_account_cannot_make_a_second_character(
    client_factory: Callable[[], TestClient],
):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    created = create_character(owner, campaign_id, name="Ada")
    assert created.status_code == 303
    _close_opening(owner, campaign_id)
    again = create_character(owner, campaign_id, name="Ada Two")
    assert again.status_code == 400
    state = open_campaign(owner.app, campaign_id)
    assert [c.name for c in state.pcs()] == ["Ada"]


def test_a_typing_player_is_shown_to_the_rest_of_the_table(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    guest_id = _account(owner.app, GUEST).id
    token = csrf_token(guest, "/campaigns")
    typing = guest.post(
        f"/campaigns/{campaign_id}/typing",
        data={"csrf_token": token, "active": "1", "name": "Brenna"},
        follow_redirects=False,
    )
    assert typing.status_code == 204
    snapshot = owner.get(f"/campaigns/{campaign_id}/turn-status").json()
    assert snapshot["typing"] == [{"id": guest_id, "name": "Brenna"}]
    # The page carries the endpoint and the note the composer's script uses.
    page = owner.get(f"/campaigns/{campaign_id}").text
    assert f'data-typing-url="/campaigns/{campaign_id}/typing"' in page
    assert "data-typing-note" in page

    # Clearing the box or sending the message stops the hint at once.
    stopped = guest.post(
        f"/campaigns/{campaign_id}/typing",
        data={"csrf_token": token, "active": "0"},
        follow_redirects=False,
    )
    assert stopped.status_code == 204
    assert owner.get(f"/campaigns/{campaign_id}/turn-status").json()["typing"] == []


def test_a_single_player_campaign_has_no_typing_control(
    client_factory: Callable[[], TestClient],
):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    assert create_character(owner, campaign_id, name="Ada").status_code == 303
    _close_opening(owner, campaign_id)
    page = owner.get(f"/campaigns/{campaign_id}").text
    assert "data-typing-url" not in page


def test_typing_is_limited_to_the_campaigns_members(
    client_factory: Callable[[], TestClient],
):
    owner, _guest, campaign_id, _ = _joined_campaign(client_factory)
    stranger = signed_in_client(client_factory, "stranger@example.com")
    response = stranger.post(
        f"/campaigns/{campaign_id}/typing",
        data={
            "csrf_token": csrf_token(stranger, "/campaigns"),
            "active": "1",
            "name": "Mallory",
        },
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert owner.get(f"/campaigns/{campaign_id}/turn-status").json()["typing"] == []


def test_a_running_turn_is_shown_to_every_page(
    client_factory: Callable[[], TestClient],
):
    """A second device sees the GM working, its status line, and the reply as it is written."""
    client = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(client)
    assert create_character(client, campaign_id, name="Ada").status_code == 303

    # The campaign's opening gives it an open turn; the busy block stands in for the reply
    # that a first device is streaming right now.
    with campaigns_service._exclusive(campaign_id):
        campaigns_service.set_turn_activity(campaign_id, "Narrating...")
        campaigns_service.append_turn_reply(campaign_id, "The door creaks open.")

        snapshot = client.get(f"/campaigns/{campaign_id}/turn-status").json()
        assert snapshot["status"] == "busy"
        assert snapshot["running"] is True
        assert snapshot["activity"] == "Narrating..."
        assert snapshot["reply"] == "The door creaks open."

        page = client.get(f"/campaigns/{campaign_id}/live")
        assert page.status_code == 200
        assert 'data-gm-state="busy"' in page.text
        assert "data-gm-working" in page.text
        assert "Narrating..." in page.text
        assert "The door creaks open." in page.text
        assert "data-jump-to-bottom" in page.text
        composer = page.text.split('id="composer"', 1)[1]
        assert "<textarea" in composer
        assert "disabled" in composer.split("</form>", 1)[0]


def test_the_live_stream_carries_ai_activity_and_the_streamed_reply(
    client_factory: Callable[[], TestClient],
):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)
    assert create_character(owner, campaign_id, name="Ada").status_code == 303
    engine = owner.app.state.engine

    async def snapshots():
        with campaigns_service._exclusive(campaign_id):
            campaigns_service.set_turn_activity(campaign_id, "Narrating...")
            campaigns_service.append_turn_reply(campaign_id, "The door creaks open.")
            stream = campaigns_web._event_lines(engine, campaign_id, owner.app.state.typing)
            first = await anext(stream)
            campaigns_service.set_turn_activity(campaign_id, "Writing dialogue...")
            campaigns_service.append_turn_reply(campaign_id, " A voice answers.")
            second = await anext(stream)
            await stream.aclose()
            return first, second

    first, second = asyncio.run(snapshots())
    first_payload = json.loads(first.removeprefix("data: "))
    second_payload = json.loads(second.removeprefix("data: "))
    assert first_payload["status"] == "busy"
    assert first_payload["running"] is True
    assert first_payload["activity"] == "Narrating..."
    assert first_payload["reply"] == "The door creaks open."
    assert second_payload["activity"] == "Writing dialogue..."
    assert second_payload["reply"] == "The door creaks open. A voice answers."


def test_the_turn_stream_records_activity_and_the_partial_reply(
    client_factory: Callable[[], TestClient],
):
    owner = signed_in_client(client_factory, OWNER)
    campaign_id = create_campaign(owner)

    async def run():
        async def events():
            yield campaigns_service.Status("Narrating...")
            yield campaigns_service.Delta("The door ")
            yield campaigns_service.Delta("creaks.")
            assert campaigns_service.turn_progress(campaign_id) == (
                "Narrating...",
                "The door creaks.",
            )
            yield campaigns_service.Retract()
            assert campaigns_service.turn_progress(campaign_id) == ("Narrating...", "")
            yield campaigns_service.Delta("A new sentence.")
            yield campaigns_service.Saved(1)
            assert campaigns_service.turn_progress(campaign_id) == ("Narrating...", "")

        with campaigns_service._exclusive(campaign_id):
            lines = [line async for line in campaigns_web._turn_lines(events(), campaign_id)]
        assert lines[0] == '{"type": "status", "text": "Narrating..."}\n'

    asyncio.run(run())


def test_the_live_stream_carries_typing_without_a_campaign_event(
    client_factory: Callable[[], TestClient],
):
    owner, _guest, campaign_id, _ = _joined_campaign(client_factory)
    app = owner.app
    guest_id = _account(app, GUEST).id
    owner_id = _account(app, OWNER).id
    typing = app.state.typing
    typing.note(campaign_id, guest_id, "Brenna")

    async def snapshots():
        stream = campaigns_web._event_lines(app.state.engine, campaign_id, typing)
        first = await anext(stream)
        # A second player starts typing; no campaign event or turn change happens.
        typing.note(campaign_id, owner_id, "Ada")
        second = await anext(stream)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(snapshots())
    assert json.loads(first.removeprefix("data: "))["typing"] == [
        {"id": guest_id, "name": "Brenna"}
    ]
    assert json.loads(second.removeprefix("data: "))["typing"] == [
        {"id": guest_id, "name": "Brenna"},
        {"id": owner_id, "name": "Ada"},
    ]
