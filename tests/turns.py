"""Encounter turn helpers for tests.

An encounter runs on turns: an NPC's turn waits for the player to press Start, a player
character's turn starts automatically, and an NPC's turn ends when the GM's reply finishes.
Combat tools refuse an attack by a character who is not taking their turn, so a test that attacks
sets up the turn the table would have. These helpers drive the same engine calls the page does,
so a test never reaches past the rules it is checking.
"""

from __future__ import annotations

from sqlmodel import Session

from cygnusloom.services import encounters
from cygnusloom.services.events import (
    PC_KIND,
    ChatGM,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.world import WorldTools


def close_reply(db: Session, campaign_id: int) -> None:
    """Finish the GM's reply, as the end of a turn does, so the next turn can begin."""
    if load_state(db, campaign_id).turn is not None:
        record(db, campaign_id, ChatGM(content="The reply finishes.", model="test"))
        db.commit()


def next_turn(tools: WorldTools, name: str) -> None:
    """End whatever turn is running and come back round to `name`'s next turn. A test that makes
    more than one attack by the same character uses this, because a turn allows only the rule
    set's attack actions; two attacks need two turns."""
    with Session(tools.engine) as db:
        close_reply(db, tools.campaign_id)
        state = load_state(db, tools.campaign_id)
        encounter = state.encounter
        assert encounter is not None, "there is no encounter to take a turn in"
        if encounter.acting is not None:
            acting = state.characters[name_key(encounter.acting)]
            encounters.end_turn(
                db, tools.campaign_id, acting.name, by_player=acting.kind == PC_KIND
            )
    take_turn(tools, name)


def take_turn(tools: WorldTools, name: str) -> None:
    """Put the encounter's turn on `name`, ending the turns in between and closing each GM reply
    the way the table would by pressing Start and End your turn. The name must be in the order."""
    with Session(tools.engine) as db:
        close_reply(db, tools.campaign_id)
        state = load_state(db, tools.campaign_id)
        encounter = state.encounter
        assert encounter is not None, "there is no encounter to take a turn in"
        for _ in range(len(encounter.order) + 1):
            state = load_state(db, tools.campaign_id)
            encounter = state.encounter
            assert encounter is not None
            if encounter.acting is not None:
                acting = state.characters[name_key(encounter.acting)]
                if name_key(acting.name) == name_key(name):
                    return
                encounters.end_turn(
                    db, tools.campaign_id, acting.name, by_player=acting.kind == PC_KIND
                )
                continue
            upcoming = state.characters[name_key(encounter.order[encounter.up])]
            encounters.begin_turn(db, tools.campaign_id, upcoming.name)
            close_reply(db, tools.campaign_id)
            if name_key(upcoming.name) == name_key(name):
                return
            encounters.end_turn(
                db, tools.campaign_id, upcoming.name, by_player=upcoming.kind == PC_KIND
            )
        raise AssertionError(f"{name} never came up in the encounter order")
