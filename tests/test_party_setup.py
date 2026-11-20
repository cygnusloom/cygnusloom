"""The starting party: the player builds their character and hirelings before play begins.

The creation page keeps the campaign in setup until the player starts the game, showing one tab
per party member. Hirelings use the same creation options as the player character, receive
their chosen starting kit, serve at wage zero, and are played by the player. Removal before
play deletes the character and the gear they carried; once play begins the party is fixed.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.services.events import load_state, name_key
from tests.conftest import csrf_token, remember_csrf_token
from tests.factories import create_campaign_direct, create_character, signed_in_client
from tests.test_campaigns import _create_character, _new_campaign
from tests.test_character_creation import (
    _cairn_campaign,
    _choices,
    _complete_form,
    _rolled_stats,
)
from tests.test_start_level import LEVELS
from tests.test_wwn_advancement import _creation_fields


def _state(app, campaign_id: int):
    with Session(app.state.engine) as session:
        return load_state(session, campaign_id)


def _party_page(client: TestClient, campaign_id: int, active: str = "") -> str:
    suffix = f"?party={active}" if active else ""
    return client.get(f"/campaigns/{campaign_id}{suffix}").text


def _hireling_form(client: TestClient, campaign_id: int, name: str, **fields: str):
    """Fill the add-hireling tab from its own page, then submit it."""
    page = _party_page(client, campaign_id, "add")
    token = remember_csrf_token(client, page)
    form = {
        "csrf_token": token,
        "name": name,
        "pronouns": "they/them",
        "template-role": "test-role",
        "field-post": "Camp",
        **fields,
    }
    return client.post(
        f"/campaigns/{campaign_id}/party/hireling",
        data=form,
        follow_redirects=False,
    )


def _start(client: TestClient, campaign_id: int):
    return client.post(
        f"/campaigns/{campaign_id}/start",
        data={"csrf_token": csrf_token(client, f"/campaigns/{campaign_id}")},
        follow_redirects=False,
    )


def _remove(client: TestClient, campaign_id: int, name: str):
    return client.post(
        f"/campaigns/{campaign_id}/party/remove",
        data={
            "csrf_token": csrf_token(client, f"/campaigns/{campaign_id}"),
            "name": name,
        },
        follow_redirects=False,
    )


def test_creating_a_character_leaves_the_starting_party_open(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)

    created = create_character(client, campaign_id, start=False, templates={"role": "test-role"})

    assert created.status_code == 303
    state = _state(client.app, campaign_id)
    assert not state.opened
    assert state.turn is None
    page = _party_page(client, campaign_id)
    assert "Your party" in page
    assert "party-tabs" in page
    assert "+ Add a hireling" in page
    assert "Start the game" in page
    assert ">Ada <" in page


def test_the_party_page_lists_members_without_showing_the_character_form(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})

    page = _party_page(client, campaign_id)

    assert "Create character" not in page
    assert "Blank skill and feature picks" not in page


def test_the_add_hireling_form_does_not_show_the_replacement_screen(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})

    page = _party_page(client, campaign_id, "add")

    assert "is dead" not in page
    assert f'action="/campaigns/{campaign_id}/party/hireling"' in page
    assert "Add to the party" in page


def test_a_hireling_joins_from_the_same_creation_options(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})

    added = _hireling_form(client, campaign_id, "Bram")

    assert added.status_code == 303
    state = _state(client.app, campaign_id)
    hireling = state.characters[name_key("Bram")]
    assert hireling.hireling
    assert hireling.employer == "Ada"
    assert hireling.wage == 0
    assert hireling.kind == "pc"
    assert hireling.templates == {"role": "test-role"}
    assert hireling.fields.get("post") == "Camp"
    assert hireling.location is None
    page = _party_page(client, campaign_id, "Bram")
    assert "Bram" in page
    assert "Hireling of Ada" in page
    assert "Remove Bram" in page


def test_a_starting_hireling_brings_the_starting_kit(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _cairn_campaign(client_factory)
    created = create_character(
        client, campaign_id, name="Ada", fields=_complete_form(client, campaign_id), start=False
    )
    assert created.status_code == 303
    page = _party_page(client, campaign_id, "add")
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    token = remember_csrf_token(client, page)
    added = client.post(
        f"/campaigns/{campaign_id}/party/hireling",
        data={"csrf_token": token, "name": "Bram", "pronouns": "they/them", **fields},
        follow_redirects=False,
    )

    assert added.status_code == 303
    hireling = _state(client.app, campaign_id).characters[name_key("Bram")]
    assert hireling.hireling
    assert hireling.items
    assert hireling.armor >= 0


def test_a_hireling_can_be_removed_with_their_gear_before_play(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})
    _hireling_form(client, campaign_id, "Bram")

    removed = _remove(client, campaign_id, "Bram")

    assert removed.status_code == 303
    state = _state(client.app, campaign_id)
    assert "Bram" not in {c.name for c in state.characters.values()}
    assert f'href="/campaigns/{campaign_id}?party=Bram"' not in _party_page(
        client, campaign_id
    )


def test_the_player_character_cannot_be_removed(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})

    removed = _remove(client, campaign_id, "Ada")

    assert removed.status_code == 303
    assert "Ada" in {c.name for c in _state(client.app, campaign_id).characters.values()}


def test_a_starting_hireling_begins_at_the_campaign_starting_level(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    fields = _creation_fields(client, campaign_id)
    created = _create_character(
        client,
        campaign_id,
        name="Wren",
        fields={**fields, "start_level": "2"},
        start=False,
    )
    assert created.status_code == 303
    hireling_fields = _creation_fields(client, campaign_id, path="?party=add")
    added = client.post(
        f"/campaigns/{campaign_id}/party/hireling",
        data={
            "csrf_token": csrf_token(client, f"/campaigns/{campaign_id}?party=add"),
            "name": "Bram",
            "pronouns": "he/him",
            **hireling_fields,
        },
        follow_redirects=False,
    )

    assert added.status_code == 303
    assert added.headers["location"].startswith(
        f"/campaigns/{campaign_id}/advancement?character=Bram"
    )
    state = _state(client.app, campaign_id)
    hireling = state.characters[name_key("Bram")]
    rate = state.options.get(LEVELS.rate_option) or next(iter(LEVELS.tracks))
    assert hireling.counters.get(LEVELS.xp_counter, 0) == LEVELS.tracks[rate][1]


def test_starting_the_game_records_the_opening_and_fixes_the_party(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})
    _hireling_form(client, campaign_id, "Bram")

    started = _start(client, campaign_id)

    assert started.status_code == 303
    state = _state(client.app, campaign_id)
    assert state.opened
    assert state.turn is not None and state.turn.opening
    refused = _remove(client, campaign_id, "Bram")
    assert refused.status_code == 303
    assert "Bram" in {c.name for c in _state(client.app, campaign_id).characters.values()}


def test_a_hireling_cannot_be_added_once_play_has_begun(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character(client, campaign_id, start=False, templates={"role": "test-role"})
    _start(client, campaign_id)

    added = _hireling_form(client, campaign_id, "Bram")

    assert added.status_code == 400
    assert "Bram" not in {c.name for c in _state(client.app, campaign_id).characters.values()}
