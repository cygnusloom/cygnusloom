"""A full table shares a campaign that uses the SRD 5.2.1 rule set.

Multiplayer has to survive the rule set's own combat rules: individual initiative gives every
player character their own place in the order, so a table is a sequence of turns owned by
different accounts. These tests join several accounts, play a message from each, take a fight
through a full round, follow the live stream across the handoffs, and remove a player in the
middle of the fight.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Event, User
from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services import encounters
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.events import ChatPlayer, name_key
from cygnusloom.services.membership import create_invite, is_member, list_members
from cygnusloom.web import campaigns as campaigns_web

from .conftest import csrf_token
from .factories import (
    answer,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    db,
    open_campaign,
    rules_for,
    send_message,
    signed_in_client,
    tools_for,
)

RULE_SET = "dnd-srd-2024"
WORLD = "testhollow"

EMAILS = [
    "ada@example.com",
    "brenna@example.com",
    "cora@example.com",
    "dain@example.com",
    "elsbeth@example.com",
    "fen@example.com",
]
NAMES = ["Ada", "Brenna", "Cora", "Dain", "Elsbeth", "Fen"]

# A standard SRD fighter; every account can use the same sheet because the test is about the
# table, not about class variety.
FIGHTER_FIELDS = {
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
}


def _account_id(app: FastAPI, email: str) -> int:
    with Session(app.state.engine) as session:
        return session.exec(select(User).where(User.email == email)).one().id


def _party(
    client_factory: Callable[[], TestClient], size: int = len(EMAILS)
) -> tuple[list[TestClient], int]:
    """A campaign owned by the first account with every other account joined through a code."""
    owner = signed_in_client(client_factory, EMAILS[0])
    campaign_id = create_campaign_direct(
        owner, world_template=WORLD, rule_set=RULE_SET, email=EMAILS[0]
    )
    create_character_direct(
        owner, campaign_id, name=NAMES[0], fields=FIGHTER_FIELDS, email=EMAILS[0]
    )
    close_opening(owner, campaign_id)
    invite = create_invite(owner.app.state.engine, _account_id(owner.app, EMAILS[0]), campaign_id)
    clients = [owner]
    for email, name in zip(EMAILS[1:size], NAMES[1:size]):
        guest = signed_in_client(client_factory, email)
        token = csrf_token(guest, f"/join/{invite.code}")
        joined = guest.post(
            f"/join/{invite.code}",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert joined.status_code == 303, joined.text
        create_character_direct(guest, campaign_id, name=name, fields=FIGHTER_FIELDS, email=email)
        clients.append(guest)
    return clients, campaign_id


def _start_srd_fight(
    app: FastAPI,
    campaign_id: int,
    player_names: list[str],
    monster: str = "Ogre",
    first: str | None = None,
) -> Any:
    """An encounter with every player character and one monster, ordered players first."""
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)
    tools.create_location("Cave", "A low cave.")
    tools.create_encounter_map("Cave Yard", "Cave", 12, 12, "Open stone.")
    tools.start_encounter("Cave Yard")
    tools.create_monster(monster, "Cave", template="monster-ogre")
    for name in player_names:
        tools.add_participant(name)
    tools.add_participant(monster)
    rules = rules_for(RULE_SET)
    world = app.state.content.world_content(WORLD, RULE_SET)
    ctx = level_up_service.tool_context(app.state.engine, campaign_id, rules, world)
    dnd_srd_2024.roll_initiative(ctx)
    # The adjusted order makes its first player current, which is how a GM corrects the
    # opening order after the individual rolls.
    tools.adjust_encounter_order([*player_names, monster], current=first or player_names[0])
    return tools


def _end_turn(client: TestClient, campaign_id: int, name: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/encounter/turn/end",
        data={"csrf_token": token, "name": name},
        follow_redirects=False,
    )


def _npc_turn_stream(client: TestClient, campaign_id: int, name: str) -> list[dict[str, Any]]:
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/encounter/turn/stream",
        data={"csrf_token": token, "name": name},
    )
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _script(text: str) -> str:
    return "__FAKE__:" + json.dumps({"turns": [{"text": text}]})


def test_a_full_table_shares_one_srd_campaign(client_factory: Callable[[], TestClient], fake):
    clients, campaign_id = _party(client_factory)
    app = clients[0].app
    account_ids = [_account_id(app, email) for email in EMAILS]

    members = list_members(app.state.engine, campaign_id)
    assert [member.display_name for member in members] == [email.split("@")[0] for email in EMAILS]
    assert [member.character for member in members] == NAMES
    assert all(member.owner is (index == 0) for index, member in enumerate(members))

    # Every browser opens the shared campaign and sees the whole party.
    for client in clients:
        page = client.get(f"/campaigns/{campaign_id}")
        assert page.status_code == 200
        assert all(name in page.text for name in NAMES)
        assert "Players" in page.text
    # A member cannot use the owner's controls.
    assert clients[1].get(f"/campaigns/{campaign_id}/export").status_code == 404

    # Everyone typing at once is one table, not a private scene.
    for client, name in zip(clients, NAMES):
        response = client.post(
            f"/campaigns/{campaign_id}/typing",
            data={"csrf_token": csrf_token(client, "/campaigns"), "active": "1", "name": name},
            follow_redirects=False,
        )
        assert response.status_code == 204
    snapshot = clients[0].get(f"/campaigns/{campaign_id}/turn-status").json()
    assert {typer["name"] for typer in snapshot["typing"]} == set(NAMES)
    for client in clients:
        client.post(
            f"/campaigns/{campaign_id}/typing",
            data={"csrf_token": csrf_token(client, "/campaigns"), "active": "0"},
            follow_redirects=False,
        )

    # Six messages, each speaking for its own character, all in one story.
    for client, name in zip(clients, NAMES):
        response = send_message(client, campaign_id, _script(f"{name} looks around."))
        assert response.status_code == 303, response.text
        assert open_campaign(app, campaign_id).turn is None

    with db(app) as session:
        rows = session.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == ChatPlayer.kind)
            .order_by(Event.seq)
        ).all()
    payloads = [json.loads(row.payload) for row in rows]
    assert [payload["speaker"] for payload in payloads] == NAMES
    assert [payload["player_id"] for payload in payloads] == account_ids
    assert all("player_name" not in payload for payload in payloads)
    transcript = json.dumps(payloads)
    assert all(email not in transcript for email in EMAILS)

    # The GM is told that several accounts share the table, and it receives the newest message
    # under the account's public name rather than its email.
    conversation = json.dumps(fake.calls)
    assert "Several players share this campaign" in conversation
    assert f"{NAMES[-1]} ({EMAILS[-1].split('@')[0]})" in conversation
    assert all(email not in conversation for email in EMAILS)


def test_an_srd_fight_moves_between_players_in_initiative_order(
    app: FastAPI, client_factory: Callable[[], TestClient]
):
    clients, campaign_id = _party(client_factory, size=5)
    account_ids = [_account_id(app, email) for email in EMAILS[:5]]
    player_names = NAMES[:5]
    _start_srd_fight(app, campaign_id, player_names)

    state = open_campaign(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.fixed_order is True
    assert state.encounter.order == [*player_names, "Ogre"]
    assert state.encounter.acting == player_names[0]

    for index, name in enumerate(player_names):
        state = open_campaign(app, campaign_id)
        assert state.encounter is not None
        assert state.encounter.acting == name
        assert state.encounter.round == 1

        # Every page agrees on whose turn it is. The snapshot names the account that owns the
        # turn; the encounter panel itself names the character.
        for viewer_index, client in enumerate(clients):
            status = client.get(f"/campaigns/{campaign_id}/turn-status").json()
            assert (status["actor"], status["actor_id"]) == (
                EMAILS[index].split("@")[0],
                account_ids[index],
            )
            page = client.get(f"/campaigns/{campaign_id}").text
            if viewer_index == index:
                assert "It is your turn." in page
            elif viewer_index == 0:
                # The owner can end any player's turn for a table that lost a connection.
                assert f"End {name}" in page
                assert "turn when the table is ready to move on." in page
            else:
                assert f"{name} is taking their turn." in page
                assert "The turn ends when the GM finishes it." not in page

        # Another account cannot take this character's turn.
        other = clients[(index + 1) % len(clients)]
        refused = send_message(other, campaign_id, _script("Out of turn."))
        assert refused.status_code == 400
        assert "turn" in refused.text.lower()
        assert open_campaign(app, campaign_id).encounter.acting == name

        acted = send_message(clients[index], campaign_id, _script(f"{name} acts."))
        assert acted.status_code == 303, acted.text
        assert open_campaign(app, campaign_id).encounter.acting == name

        if index == 1:
            # The owner may end a disconnected player's turn; another guest may not.
            assert _end_turn(clients[2], campaign_id, name).status_code == 400
            ended = _end_turn(clients[0], campaign_id, name)
        else:
            ended = _end_turn(clients[index], campaign_id, name)
        assert ended.status_code == 303, ended.text

        state = open_campaign(app, campaign_id)
        assert state.encounter is not None
        assert state.encounter.round == 1
        if index < len(player_names) - 1:
            # The next player character's turn begins without another button press.
            assert state.encounter.acting == player_names[index + 1]
        else:
            # An NPC's turn waits for the table to press Start.
            assert state.encounter.acting is None
            assert state.encounter.order[state.encounter.up] == "Ogre"

    # Any member can move an NPC's turn along, so the table is not blocked on one account.
    lines = _npc_turn_stream(clients[3], campaign_id, "Ogre")
    assert lines[-1]["type"] == "saved"
    state = open_campaign(app, campaign_id)
    assert state.encounter is not None
    # The individual-initiative order is fixed for the whole fight, and round two starts at
    # the top of it with the first player again.
    assert state.encounter.round == 2
    assert state.encounter.order == [*player_names, "Ogre"]
    assert state.encounter.acting == player_names[0]


def test_the_live_stream_follows_an_srd_turn_handoff(
    app: FastAPI,
    client_factory: Callable[[], TestClient],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(campaigns_web, "EVENT_POLL_SECONDS", 0.01)
    _clients, campaign_id = _party(client_factory, size=3)
    account_ids = [_account_id(app, email) for email in EMAILS[:3]]
    player_names = NAMES[:3]
    _start_srd_fight(app, campaign_id, player_names)

    async def handoff():
        stream = campaigns_web._event_lines(app.state.engine, campaign_id, app.state.typing)
        first = json.loads((await anext(stream)).removeprefix("data: "))
        with db(app) as session:
            encounters.end_turn(session, campaign_id, player_names[0], by_player=True)
        second = json.loads((await anext(stream)).removeprefix("data: "))
        await stream.aclose()
        return first, second

    first, second = asyncio.run(handoff())
    assert (first["actor"], first["actor_id"]) == (
        EMAILS[0].split("@")[0],
        account_ids[0],
    )
    assert second["event"] > first["event"]
    assert (second["actor"], second["actor_id"]) == (
        EMAILS[1].split("@")[0],
        account_ids[1],
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "roll_check",
            {
                "prompt": "Brenna, read the road for tracks.",
                "character": "Brenna",
                "skill": "survival",
                "stat": "wis",
                "difficulty": 10,
            },
        ),
        (
            "player_save",
            {
                "prompt": "Brenna, keep your footing on the icy road.",
                "name": "Brenna",
                "stat": "con",
                "dc": 10,
                "cause": "the icy road",
            },
        ),
    ],
)
def test_a_prompt_for_another_players_character_belongs_to_that_player(
    app: FastAPI,
    client_factory: Callable[[], TestClient],
    tool: str,
    arguments: dict[str, Any],
):
    """The GM may ask a character other than the one who spoke to make a check or save. The
    prompt then belongs to that character's player, not to the player whose message opened the
    reply; otherwise the guest cannot answer and the opener sees a roll they must not take."""
    clients, campaign_id = _party(client_factory, size=2)
    owner, guest = clients
    guest_id = _account_id(app, EMAILS[1])
    script = "__FAKE__:" + json.dumps(
        {
            "turns": [
                {
                    "rounds": [[{"name": tool, "arguments": arguments}]],
                    "text": "Brenna reads the road.",
                }
            ]
        }
    )
    response = send_message(owner, campaign_id, script)
    assert response.status_code == 303, response.text

    state = open_campaign(app, campaign_id)
    assert state.turn is not None and state.turn.prompt is not None
    assert state.turn.prompt.character == "Brenna"

    # The player whose character is named owns the roll; the opener waits.
    owner_page = owner.get(f"/campaigns/{campaign_id}").text
    guest_page = guest.get(f"/campaigns/{campaign_id}").text
    assert "data-pending-action" not in owner_page
    assert "data-pending-action" in guest_page
    assert "must answer the GM." in owner_page
    assert "Ada must answer the GM." not in owner_page
    snapshot = owner.get(f"/campaigns/{campaign_id}/turn-status").json()
    assert snapshot["status"] == "prompt"
    assert snapshot["pending_for"] == guest_id

    refused = answer(owner, campaign_id, "roll")
    assert refused[-1]["type"] == "error"
    assert refused[-1]["status"] == 400
    assert "must answer" in refused[-1]["message"]
    assert open_campaign(app, campaign_id).turn is not None

    done = answer(guest, campaign_id, "roll")
    assert done[-1]["type"] == "saved"
    assert open_campaign(app, campaign_id).turn is None


def test_removing_a_player_mid_fight_hands_their_turn_on(
    app: FastAPI, client_factory: Callable[[], TestClient]
):
    clients, campaign_id = _party(client_factory, size=3)
    account_ids = [_account_id(app, email) for email in EMAILS[:3]]
    player_names = NAMES[:3]
    _start_srd_fight(app, campaign_id, player_names, first=player_names[1])
    assert open_campaign(app, campaign_id).encounter.acting == player_names[1]

    response = clients[0].post(
        f"/campaigns/{campaign_id}/members/{account_ids[1]}/remove",
        data={"csrf_token": csrf_token(clients[0], "/campaigns"), "character": "retire"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    # The removed account loses the table, and the fight moves to the next player.
    assert clients[1].get(f"/campaigns/{campaign_id}").status_code == 404
    state = open_campaign(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.acting == player_names[2]
    assert state.encounter.order == [player_names[0], player_names[2], "Ogre"]
    assert state.characters[name_key(player_names[1])].retired

    # An active join code does not undo the removal.
    invite = create_invite(app.state.engine, account_ids[0], campaign_id)
    assert clients[1].get(f"/join/{invite.code}").status_code == 403
    blocked = clients[1].post(
        f"/join/{invite.code}",
        data={"csrf_token": csrf_token(clients[1], "/campaigns")},
        follow_redirects=False,
    )
    assert blocked.status_code == 403

    # The owner can invite the account back, and the fight keeps going.
    readded = clients[0].post(
        f"/campaigns/{campaign_id}/members/{account_ids[1]}/readd",
        data={"csrf_token": csrf_token(clients[0], "/campaigns")},
        follow_redirects=False,
    )
    assert readded.status_code == 303
    assert is_member(app.state.engine, account_ids[1], campaign_id)
    assert _end_turn(clients[2], campaign_id, player_names[2]).status_code == 303
    state = open_campaign(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.acting is None
    assert state.encounter.order[state.encounter.up] == "Ogre"
