from __future__ import annotations

import html
import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.services import campaigns as campaigns_service
from cygnusloom.services import world
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state
from tests.test_campaigns import _create_character, _new_campaign, _send, _signed_in

STAT_SELECT_RE = re.compile(r'<select name="stat-(\w+)"[^>]*>(.*?)</select>', re.DOTALL)
STAT_HIDDEN_RE = re.compile(r'<input type="hidden" name="stat-(\w+)" value="(\d+)">')
RADIO_RE = re.compile(
    r'<input type="radio" name="((?:template|field|campaign)-[\w-]+)" value="([^"]*)"'
)
PICK_SELECT_RE = re.compile(r'<select name="(pick-[\w-]+)"[^>]*>(.*?)</select>', re.DOTALL)
OPTION_VALUE_RE = re.compile(r'<option value="([^"]*)"([^>]*)>')
CAMPAIGN_RADIO_RE = re.compile(r'<input type="radio" name="(campaign-[\w-]+)" value="([^"]*)"')


def _cairn_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A Cairn campaign whose shared options are chosen, so the character is next."""
    client = _signed_in(client_factory)
    campaign_id = _new_cairn_campaign(client)
    _choose_omen(client, campaign_id)
    return client, campaign_id


def _new_cairn_campaign(client: TestClient) -> int:
    """A Cairn campaign with nothing chosen yet, so its options and character are both pending."""
    response = _new_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def _choose_omen(client: TestClient, campaign_id: int) -> None:
    """Record the shared campaign options, as the creation form does, with the first omen."""
    cairn = client.app.state.content.rule_sets["cairn2e"]
    omen = next(iter(cairn.campaign.tables["omen"])).value
    with Session(client.app.state.engine) as db:
        world.set_campaign_options(db, campaign_id, cairn.campaign, {"omen": omen})
        db.commit()


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


def _picks(page: str) -> dict[str, str]:
    """One choice for every pick select, the way a player works down the lists: the first select
    of a group takes the first option, the second the second, and so on, so no pick is left for
    the server to roll. Tests that check a particular pick set it themselves afterwards."""
    picks: dict[str, str] = {}
    for name, body in PICK_SELECT_RE.findall(page):
        options = [
            html.unescape(value)
            for value, attributes in OPTION_VALUE_RE.findall(body)
            if value and "disabled" not in attributes
        ]
        if not options:
            continue
        index = name.rpartition("-")[2]
        position = int(index) if index.isdigit() else 0
        picks[name] = options[min(position, len(options) - 1)]
    return picks


def _named_picks(fields: dict[str, str]) -> dict[str, str]:
    """The skill and feature picks a test set by their generic name, named for the templates the
    form chose. The server reads a template's own pick name before the generic one, so a pin
    would otherwise lose to the default the form carries for every template."""
    chosen = [value for name, value in fields.items() if name.startswith("template-") and value]
    named = dict(fields)
    for name, value in fields.items():
        for prefix in ("pick-skill-", "pick-feature-"):
            if not name.startswith(prefix):
                continue
            pick, _, index = name[len(prefix) :].rpartition("-")
            if not index.isdigit() or "-" in pick:
                continue
            for template_id in chosen:
                named[f"{prefix}{template_id}-{pick}-{index}"] = value
    return named


def _campaign_choices(page: str) -> dict[str, str]:
    """The first card of every campaign option, as the creation form sends them."""
    choices: dict[str, str] = {}
    for name, value in CAMPAIGN_RADIO_RE.findall(page):
        choices.setdefault(name, html.unescape(value))
    return choices


def test_cairn_campaign_shows_the_creation_form_and_no_chat(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Create your character" in page
    assert 'name="stat-str"' in page
    assert "Twin Lakes" in page
    assert "Cairn 2e" in page
    assert f"/campaigns/{campaign_id}/messages" not in page


def test_character_creation_keeps_the_sidebar_but_not_the_chat(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="view creation"' in page
    assert 'class="sidebar"' in page
    assert 'data-view-target="journal"' in page
    assert "data-side-toggle" not in page
    assert 'class="workspace"' not in page
    assert 'class="chat"' not in page
    assert 'class="side"' not in page
    assert "/static/creation.js" in page


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
    assert not re.search(r'value="random"', page, re.IGNORECASE)
    assert '<option value="">' not in page
    assert 'data-randomize="all"' in page
    assert 'data-randomize="unchosen"' in page


def test_stats_have_their_own_randomize_button_and_rolled_swap_groups(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count("data-randomize-stats") == 1
    assert 'data-stat-group="attributes"' in page
    assert "data-stat-rolled=" in page
    assert "data-stat-assigned" not in page


def test_name_dice_draws_on_the_suggested_names_of_the_backgrounds(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-name-dice="name"' in page
    assert 'data-name-source="scrivener"' in page
    assert 'data-choice-panel="field-scrivener-names"' not in page
    assert 'data-dice-for="field-scrivener-names"' not in page


def test_every_choice_opens_a_dialog_and_radios_are_not_required(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    groups = set(re.findall(r'<input type="radio" name="([\w-]+)"', page))
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


SCRIVENER_WORK = "scrivener-what-work-did-you-keep-for-yourself"
WILD_TONGUE = "The Wild Tongue"
WILD_TONGUE_DETAIL = (
    "A bundle of leather-bound scrolls. A seminal work, cataloging the hidden languages "
    "of beasts and how to understand them."
)


def test_named_options_show_their_name_as_a_title_and_description(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f'data-title="{WILD_TONGUE}"' in page
    assert f"<span>{WILD_TONGUE}</span>" in page
    assert f'<span class="muted choice-detail">{WILD_TONGUE_DETAIL}</span>' in page


def test_a_named_option_is_stored_by_its_name(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields["template-background"] = "scrivener"
    fields[f"field-{SCRIVENER_WORK}"] = WILD_TONGUE
    assert _create_character(client, campaign_id, name="Mara", fields=fields).status_code == 303

    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    assert pc.fields[SCRIVENER_WORK] == WILD_TONGUE


def test_cairn_campaign_asks_for_its_options_and_character_on_one_page(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    campaign_id = _new_cairn_campaign(client)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="creation-form"') == 1
    assert f'action="/campaigns/{campaign_id}/creation"' in page
    assert "Campaign options" in page
    assert "Your character" in page
    assert 'name="campaign-omen"' in page
    assert 'name="setting" rows="6" maxlength="4000"' in page
    assert page.index("Campaign options") < page.index('name="setting"')
    assert page.index('name="setting"') < page.index("Your character")
    assert 'name="stat-str"' in page
    assert page.count('data-randomize="all"') == 1
    assert page.count('data-randomize="unchosen"') == 1
    assert "Create campaign and character" in page


def test_a_rule_set_without_campaign_options_still_offers_the_setting(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Campaign options" in page
    assert 'name="campaign-' not in page
    assert 'name="setting" rows="6" maxlength="4000"' in page
    assert page.index("Campaign options") < page.index('name="setting"')
    assert "Your character" in page


def test_the_campaign_options_must_all_be_chosen(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    campaign_id = _new_cairn_campaign(client)
    fields = {
        key: value
        for key, value in _complete_form(client, campaign_id).items()
        if not key.startswith("campaign-")
    }
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert "Choose Omen" in response.text
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.options == {}
    assert state.pc() is None


def test_options_and_character_are_saved_together_from_one_form(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    campaign_id = _new_cairn_campaign(client)
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {**_complete_form(client, campaign_id), **_campaign_choices(page)}
    assert _create_character(client, campaign_id, name="Mara", fields=fields).status_code == 303

    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.options["omen"] == fields["campaign-omen"]
    assert state.pc() is not None and state.pc().name == "Mara"


def test_a_failed_character_leaves_the_campaign_options_unsaved(
    client_factory: Callable[[], TestClient],
    monkeypatch: pytest.MonkeyPatch,
):
    client = _signed_in(client_factory)
    campaign_id = _new_cairn_campaign(client)
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {**_complete_form(client, campaign_id), **_campaign_choices(page)}

    def refuse(*args, **kwargs):
        raise InvalidInput("the character could not be made")

    monkeypatch.setattr(campaigns_service, "create_player_character", refuse)
    response = _create_character(client, campaign_id, name="Mara", fields=fields)
    assert response.status_code == 400
    assert "the character could not be made" in response.text
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.options == {}
    assert state.pc() is None


def test_campaign_options_are_chosen_once(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    cairn = client.app.state.content.rule_sets["cairn2e"]
    with Session(client.app.state.engine) as db:
        omen = load_state(db, campaign_id).options["omen"]
    other = next(option.value for option in cairn.campaign.tables["omen"] if option.value != omen)
    fields = {**_complete_form(client, campaign_id), "campaign-omen": other}
    assert _create_character(client, campaign_id, name="Mara", fields=fields).status_code == 303

    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.options["omen"] == omen
    assert state.pc() is not None


def test_the_omen_is_a_campaign_option_given_to_the_gm(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    cairn = client.app.state.content.rule_sets["cairn2e"]
    with Session(client.app.state.engine) as db:
        omen = load_state(db, campaign_id).options["omen"]
    assert omen in {option.value for option in cairn.campaign.tables["omen"]}
    assert "omen" not in cairn.character.fields

    assert (
        _create_character(
            client, campaign_id, name="Mara", fields=_complete_form(client, campaign_id)
        ).status_code
        == 303
    )
    with Session(client.app.state.engine) as db:
        text = world.world_state_text(load_state(db, campaign_id), cairn.campaign)
    assert f"Campaign omen: {omen}." in text


def _stored_pc(client: TestClient, campaign_id: int):
    with Session(client.app.state.engine) as db:
        return load_state(db, campaign_id).pc()


def test_pronouns_are_required_for_a_player_character(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    fields = _complete_form(client, campaign_id)

    response = _create_character(client, campaign_id, name="Mara", fields=fields, pronouns=None)
    assert response.status_code == 400
    assert "Choose your pronouns" in response.text

    response = _create_character(client, campaign_id, name="Mara", fields=fields, pronouns="")
    assert response.status_code == 400
    assert "Choose your pronouns" in response.text
    assert _stored_pc(client, campaign_id) is None


def test_other_pronouns_must_be_written(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    fields = _complete_form(client, campaign_id)
    fields["pronouns-other"] = "   "

    response = _create_character(client, campaign_id, name="Mara", fields=fields, pronouns="other")
    assert response.status_code == 400
    assert "Write your pronouns" in response.text
    assert _stored_pc(client, campaign_id) is None


def test_listed_pronouns_are_stored_and_shown_to_the_gm_and_on_the_sheet(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client,
        campaign_id,
        name="Mara",
        fields=_complete_form(client, campaign_id),
        pronouns="they/them",
    )
    assert created.status_code == 303

    pc = _stored_pc(client, campaign_id)
    assert pc is not None and pc.pronouns == "they/them"
    with Session(client.app.state.engine) as db:
        text = world.world_state_text(
            load_state(db, campaign_id), client.app.state.content.rule_sets["cairn2e"].campaign
        )
    assert "Pronouns: Mara, they/them." in text
    assert "Pronouns: they/them." in client.get(f"/campaigns/{campaign_id}").text


def test_other_pronouns_are_stored_as_written(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    fields = _complete_form(client, campaign_id)
    fields["pronouns-other"] = "  xe/xem  "

    created = _create_character(client, campaign_id, name="Mara", fields=fields, pronouns="other")
    assert created.status_code == 303
    pc = _stored_pc(client, campaign_id)
    assert pc is not None and pc.pronouns == "xe/xem"


def test_pronoun_choices_put_other_last_and_freeform_so_randomize_skips_it(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-choice-panel="pronouns"' in page
    assert 'type="radio" name="pronouns" value="he/him"' in page
    assert 'type="radio" name="pronouns" value="other" data-title="Other" data-freeform>' in page
    assert page.count("data-freeform") == 1
    assert 'name="pronouns-other"' in page
