"""Secret rolls and secret check causes stay out of the player's default transcript."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, Event
from cygnusloom.services import turns as turns_service
from cygnusloom.services.campaigns import campaign_transcript
from cygnusloom.services.events import (
    CheckMade,
    DiceRolled,
    event_hidden,
    record,
    roll_breakdown,
)
from tests.test_campaigns import _campaign_id
from tests.test_encounters import _call
from tests.test_wwn_advancement import NAME, _campaign, _world_tools


def _newest(app: FastAPI, campaign_id: int, kind: str) -> Event:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == kind)
            .order_by(Event.id)
        ).all()
    assert rows
    return rows[-1]


def test_the_engine_can_hide_a_gm_only_dice_roll(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            DiceRolled(
                dice="1d20+6",
                values=[13],
                total=19,
                reason=(
                    "Stealth for the hidden Cragmaw goblins, rolled once for the whole band "
                    "against the party's passive Perception"
                ),
                secret=True,
            ),
        )
        db.commit()
        owner_id = db.get(Campaign, campaign_id).owner_id

    lines = {
        item.content: item.hidden
        for item in campaign_transcript(app.state.engine, owner_id, campaign_id)
        if item.role == "log"
    }
    line = next(text for text in lines if text.startswith("Rolled 1d20+6"))
    assert lines[line] is True
    assert "hidden Cragmaw goblins" in line


def test_the_live_log_marks_a_secret_roll_hidden(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, DiceRolled(dice="1d6", values=[4], total=4, reason="openly"))
        record(
            db,
            campaign_id,
            DiceRolled(dice="1d20+6", values=[13], total=19, reason="a hidden watcher", secret=True),
        )
        db.commit()

    flushed = turns_service._flush(
        SimpleNamespace(engine=app.state.engine, campaign_id=campaign_id, cursor=0)
    )

    assert [(entry.text, entry.hidden) for entry in flushed][-2:] == [
        ("Rolled 1d6: 4 = 4. (openly)", False),
        ("Rolled 1d20+6: 13 = 19. (a hidden watcher)", True),
    ]


def test_the_roll_dice_tool_records_a_secret_roll(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign_id(client_factory)

    result = _call(
        app,
        campaign_id,
        "roll_dice",
        {
            "dice": "1d20+6",
            "reason": "Stealth for the hidden Cragmaw goblins",
            "secret": True,
        },
    )

    assert result["secret"] is True
    event = _newest(app, campaign_id, "dice.rolled")
    assert '"secret": true' in str(event.payload)
    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id
    line = next(
        item
        for item in campaign_transcript(app.state.engine, owner_id, campaign_id)
        if isinstance(item.content, str) and item.content.startswith("Rolled 1d20")
    )
    assert line.hidden is True


def _check(*, total: int, success: bool, secret: bool) -> CheckMade:
    return CheckMade(
        name="Hesta",
        check="Ability check",
        skill="perception",
        skill_name="Perception",
        stat="wis",
        stat_name="Wisdom",
        roll=total - 5,
        bonus=5,
        total=total,
        difficulty=19,
        success=success,
        cause="Spotting the hidden Cragmaw goblins (Stealth 19)",
        stat_bonus=5,
        dice="1d20",
        rolls=[total - 5],
        breakdown=roll_breakdown(
            "1d20",
            [total - 5],
            [("Wisdom", 5)],
            total,
            target=19,
            target_label="difficulty",
            note="the player's roll",
        ),
        secret=secret,
    )


def test_a_secret_check_keeps_the_players_dice_and_outcome_but_not_the_cause():
    line = _check(total=23, success=True, secret=True).render()
    assert line == (
        "Hesta makes an Ability check: 1d20 18 (the player's roll) +5 Wisdom = 23 and succeeds."
    )


def test_a_failed_secret_check_does_not_reveal_the_cause_or_the_difficulty():
    line = _check(total=14, success=False, secret=True).render()
    assert line == (
        "Hesta makes an Ability check: 1d20 9 (the player's roll) +5 Wisdom = 14 and fails."
    )
    assert "Stealth" not in line
    assert "difficulty" not in line


def test_an_ordinary_check_still_shows_its_cause_and_difficulty():
    line = _check(total=23, success=True, secret=False).render()
    assert "vs difficulty 19" in line
    assert "(Spotting the hidden Cragmaw goblins (Stealth 19))" in line


def test_the_roll_check_tool_marks_a_secret_check_and_its_reroll(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    result = tools.roll_check(
        "skill",
        NAME,
        skill="notice",
        stat="wis",
        difficulty=9,
        cause="Spotting the hidden Cragmaw goblins (Stealth 19)",
        secret=True,
    )

    assert result["secret"] is True
    event = _newest(app, campaign_id, "check.made")
    assert '"secret": true' in str(event.payload)


def test_a_secret_roll_is_hidden_but_a_secret_check_stays_in_the_log():
    assert event_hidden(DiceRolled(dice="1d6", values=[1], total=1, secret=True)) is True
    assert event_hidden(DiceRolled(dice="1d6", values=[1], total=1)) is False
    assert event_hidden(_check(total=23, success=True, secret=True)) is False
    assert event_hidden(_check(total=23, success=True, secret=False)) is False
