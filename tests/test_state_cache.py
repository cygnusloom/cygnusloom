"""The folded campaign state is cached and only new events are applied.

A turn folds the log a dozen times or more. The cache must behave exactly like a full fold
after appends, after a rollback, and for a view before an event, and must never hand two
callers the same mutable state.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from cygnusloom.db.models import Campaign, User
from cygnusloom.db.session import create_schema, make_engine
from cygnusloom.services import events
from cygnusloom.services.events import (
    NoteAdded,
    load_state,
    record,
    truncate,
)


@pytest.fixture
def campaign(tmp_path) -> tuple[Engine, int]:
    engine = make_engine(f"sqlite:///{tmp_path / 'state-cache.db'}")
    create_schema(engine)
    with Session(engine) as db:
        user = User(issuer="local", subject="owner", display_name="Owner")
        db.add(user)
        db.commit()
        db.refresh(user)
        campaign = Campaign(
            owner_id=user.id or 0,
            name="Cache",
            world_template="unwritten-world",
            rule_set="worldswithoutnumber",
            model="fake/scripted",
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        campaign_id = campaign.id or 0
        for index in range(3):
            record(db, campaign_id, NoteAdded(text=f"note {index}"))
        db.commit()
    yield engine, campaign_id
    events.invalidate_state_cache(engine=engine)
    engine.dispose()


def _counting_decode(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    calls = 0
    real = events.decode

    def counted(row):
        nonlocal calls
        calls += 1
        return real(row)

    monkeypatch.setattr(events, "decode", counted)
    return lambda: calls


def test_a_repeat_fold_applies_no_old_events(campaign, monkeypatch):
    engine, campaign_id = campaign
    with Session(engine) as db:
        load_state(db, campaign_id)
    calls = _counting_decode(monkeypatch)
    with Session(engine) as db:
        state = load_state(db, campaign_id)
    assert [note.text for note in state.notes] == ["note 0", "note 1", "note 2"]
    # No event was decoded: the whole fold came from the cached state.
    assert calls() == 0


def test_a_fold_after_an_append_decodes_only_the_new_event(campaign, monkeypatch):
    engine, campaign_id = campaign
    with Session(engine) as db:
        load_state(db, campaign_id)
        record(db, campaign_id, NoteAdded(text="a new note"))
        db.commit()
    calls = _counting_decode(monkeypatch)
    with Session(engine) as db:
        state = load_state(db, campaign_id)
    assert state.notes[-1].text == "a new note"
    assert calls() == 1


def test_a_view_before_an_event_matches_a_full_fold(campaign):
    engine, campaign_id = campaign
    with Session(engine) as db:
        full = load_state(db, campaign_id)
        before = full.notes[1].id
        cached_view = load_state(db, campaign_id, before=before)
        events.invalidate_state_cache(engine=engine)
        fresh_view = load_state(db, campaign_id, before=before)
    assert [note.text for note in cached_view.notes] == ["note 0"]
    assert [note.text for note in fresh_view.notes] == ["note 0"]


def test_a_rollback_is_never_served_from_the_cache(campaign, monkeypatch):
    engine, campaign_id = campaign
    with Session(engine) as db:
        state = load_state(db, campaign_id)
        third = state.notes[-1].id
        truncate(db, campaign_id, third - 1)
        db.commit()
    calls = _counting_decode(monkeypatch)
    with Session(engine) as db:
        state = load_state(db, campaign_id)
    assert [note.text for note in state.notes] == ["note 0", "note 1"]
    # The invalidated entry forced a full fold of the remaining events.
    assert calls() == 2


def test_each_caller_gets_its_own_state(campaign):
    engine, campaign_id = campaign
    with Session(engine) as db:
        first = load_state(db, campaign_id)
        first.notes.clear()
        second = load_state(db, campaign_id)
    assert [note.text for note in second.notes] == ["note 0", "note 1", "note 2"]
