from __future__ import annotations

import html
import re
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.services.events import load_state
from tests.test_campaigns import _create_character, _new_campaign, _send, _signed_in

STAT_SELECT_RE = re.compile(r'<select name="stat-(\w+)">(.*?)</select>', re.DOTALL)
STAT_HIDDEN_RE = re.compile(r'<input type="hidden" name="stat-(\w+)" value="(\d+)">')
RADIO_RE = re.compile(r'<input type="radio" name="((?:template|field)-[\w-]+)" value="([^"]*)"')


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


def _choices(page: str) -> dict[str, str]:
    """The first card of every radio group, as a player who picks the first card would send."""
    choices: dict[str, str] = {}
    for name, value in RADIO_RE.findall(page):
        choices.setdefault(name, html.unescape(value))
    return choices


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


def test_character_creation_keeps_the_sidebar_but_not_the_chat(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="view creation"' in page
    assert 'class="sidebar"' in page
    assert 'data-view-target="lore"' in page
    assert 'class="workspace"' not in page
    assert 'class="chat"' not in page
    assert 'class="side"' not in page
    assert "/static/character_create.js" in page


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

    fields = {f"stat-{stat}": str(value) for stat, value in rolled.items()}
    fields.update(_choices(client.get(f"/campaigns/{campaign_id}").text))
    created = _create_character(client, campaign_id, name="Mara", fields=fields)
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
    page = client.get(f"/campaigns/{campaign_id}").text
    rolled = _rolled_stats(page)
    fields = {f"stat-{stat}": str(value) for stat, value in rolled.items()}
    fields.update(_choices(page))
    assert _create_character(client, campaign_id, name="Mara", fields=fields).status_code == 303

    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    assert pc.location is None
    assert "not yet placed" in client.get(f"/campaigns/{campaign_id}").text


def _complete_form(client: TestClient, campaign_id: int) -> dict[str, str]:
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    return fields


def test_creation_page_offers_radio_cards_with_dice_and_no_random(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'type="radio" name="template-background"' in page
    assert 'type="radio" name="field-physique"' in page
    assert 'data-dice-for="template-background"' in page
    assert 'data-dice-for="field-physique"' in page
    assert "Random" not in page
    assert '<option value="">' not in page


def test_every_choice_opens_a_dialog_and_radios_are_not_required(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    groups = {name for name, _ in RADIO_RE.findall(page)}
    dialog_ids = re.findall(r'<dialog id="(choice-dialog-[\w-]+)"', page)
    opened = re.findall(r'data-open-choice="(choice-dialog-[\w-]+)"', page)
    assert {f"choice-dialog-{name}" for name in groups} == set(dialog_ids)
    assert sorted(dialog_ids) == sorted(opened)
    assert len(dialog_ids) == len(set(dialog_ids))
    for tag in re.findall(r'<input type="radio"[^>]*>', page):
        attributes = re.sub(r'"[^"]*"', '""', tag)
        assert " required" not in attributes


def test_every_template_and_choice_must_be_set(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    spec = client.app.state.content.rule_sets["cairn2e"].character

    fields = _complete_form(client, campaign_id)
    del fields["template-background"]
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert "Choose a background" in response.text

    fields = _complete_form(client, campaign_id)
    table = spec.templates[fields["template-background"]].tables[0]
    del fields[f"field-{table}"]
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert f"Choose {spec.table_names[table]}" in html.unescape(response.text)

    fields = _complete_form(client, campaign_id)
    del fields["field-physique"]
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert f"Choose {spec.fields['physique'].name}" in html.unescape(response.text)


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
