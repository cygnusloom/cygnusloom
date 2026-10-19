from __future__ import annotations

import re
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.services.events import load_state
from tests.test_campaigns import _create_character, _new_campaign, _send, _signed_in

STAT_SELECT_RE = re.compile(r'<select name="stat-(\w+)">(.*?)</select>', re.DOTALL)
STAT_HIDDEN_RE = re.compile(r'<input type="hidden" name="stat-(\w+)" value="(\d+)">')


def _cairn_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="kilharbor", rule_set="cairn2e")
    assert response.status_code == 303
    return client, int(response.headers["location"].rsplit("/", 1)[1])


def _rolled_stats(page: str) -> dict[str, int]:
    """The rolled values as the creation form shows them: the default of each select, or the
    value of a hidden input for a stat that cannot be swapped."""
    rolled = {
        stat: int(re.search(r'value="(\d+)" selected', body).group(1))
        for stat, body in STAT_SELECT_RE.findall(page)
    }
    rolled.update({stat: int(value) for stat, value in STAT_HIDDEN_RE.findall(page)})
    return rolled


def test_cairn_campaign_shows_the_creation_form_and_no_chat(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Create your character" in page
    assert 'name="stat-str"' in page
    assert "Kilharbor" in page
    assert "Cairn 2e" in page
    assert f"/campaigns/{campaign_id}/messages" not in page


def test_chat_is_refused_until_the_character_exists(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    campaign_id = int(_new_campaign(client).headers["location"].rsplit("/", 1)[1])
    assert _send(client, campaign_id, "Hello").status_code == 400


def test_cairn_character_is_created_from_the_rolled_stats(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    rolled = _rolled_stats(client.get(f"/campaigns/{campaign_id}").text)
    assert set(rolled) == {"str", "dex", "wil", "hp"}

    created = _create_character(
        client,
        campaign_id,
        name="Mara",
        fields={f"stat-{stat}": str(value) for stat, value in rolled.items()},
    )
    assert created.status_code == 303

    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    pc = next(c for c in state.characters.values() if c.kind == "pc")
    assert pc.name == "Mara"
    assert pc.stats == rolled
    assert f"/campaigns/{campaign_id}/messages" in client.get(f"/campaigns/{campaign_id}").text


def test_cairn_character_starts_unplaced_until_the_gm_places_it(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    rolled = _rolled_stats(client.get(f"/campaigns/{campaign_id}").text)
    fields = {f"stat-{stat}": str(value) for stat, value in rolled.items()}
    assert _create_character(client, campaign_id, name="Mara", fields=fields).status_code == 303

    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    assert pc.location is None
    assert "not yet placed" in client.get(f"/campaigns/{campaign_id}").text


def test_stat_outside_its_range_is_rejected(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    rolled = _rolled_stats(client.get(f"/campaigns/{campaign_id}").text)
    fields = {f"stat-{stat}": str(value) for stat, value in rolled.items()}
    fields["stat-str"] = "99"
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert f"/campaigns/{campaign_id}/messages" not in response.text


def test_stat_that_is_not_a_number_is_rejected(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    rolled = _rolled_stats(client.get(f"/campaigns/{campaign_id}").text)
    fields = {f"stat-{stat}": str(value) for stat, value in rolled.items()}
    fields["stat-dex"] = "lots"
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert "must be a number" in response.text


def test_a_second_player_character_is_refused(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    campaign_id = int(_new_campaign(client).headers["location"].rsplit("/", 1)[1])
    assert _create_character(client, campaign_id, name="Ada").status_code == 303
    response = _create_character(client, campaign_id, name="Bran")
    assert response.status_code != 303
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert [c.name for c in state.characters.values() if c.kind == "pc"] == ["Ada"]
