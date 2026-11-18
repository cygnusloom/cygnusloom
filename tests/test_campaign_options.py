"""Campaign options, respecs, and the details a player may change on a character.

A campaign's ruleset options are chosen with the first character and stay editable from the
settings page. The respec option lets each player rebuild their own character or a hireling they
play for. Separately, a player may rewrite the flavor details of their own player character at
any time, while a hireling's flavor belongs to the GM and only its mechanical fields are the
player's.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign
from cygnusloom.services import campaign_files
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    CharacterFieldSet,
    CharacterRespecced,
    decode,
    events_since,
)
from tests.conftest import content_library, csrf_token, remember_csrf_token
from tests.factories import (
    campaign_ready_fast,
    character_named,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    open_campaign,
    pinned_stats,
    place_pc,
    signed_in_client,
    tools_for,
)
from tests.test_campaigns import _new_campaign, _signed_in
from tests.test_character_creation import (
    _cairn_campaign,
    _choices,
    _complete_form,
    _picks,
    _rolled_stats,
)
from tests.test_multiplayer import _joined_campaign
from tests.test_start_level import _submit as _submit_advancement
from tests.test_wwn_advancement import CLASS_PICKS, NAME, _counter
from tests.test_wwn_advancement import _campaign as _wwn_campaign

CAIRN = content_library().rule_sets["cairn2e"]
WWN_LEVELS = content_library().rule_sets["worldswithoutnumber"].level_ups["level"]
FAST_XP = WWN_LEVELS.tracks["fast"]
PC = "Ada"
HIRELING = "Wen"
# The Cairn stats a rebuilding player rolls in the respec test.
RESPEC_STATS = {"str": 14, "dex": 9, "wil": 12, "hp": 6}


def _row(app: FastAPI, campaign_id: int) -> Campaign:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        db.expunge(campaign)
        return campaign


def _cairn_ready(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A Cairn campaign with its options and character made and its opening closed."""
    client, campaign_id = _cairn_campaign(client_factory)
    create_character_direct(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    close_opening(client, campaign_id)
    return client, campaign_id


def _enable_respec(app: FastAPI, campaign_id: int) -> None:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.allow_respec = True
        db.add(campaign)
        db.commit()


def _post_settings(client: TestClient, campaign_id: int, **data: str):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/settings",
        data={"csrf_token": token, **data},
        follow_redirects=False,
    )


def _settings_form(client: TestClient, campaign_id: int, **extra: str) -> dict[str, str]:
    row = _row(client.app, campaign_id)
    return {
        "model": row.model,
        "label": row.name,
        "use_inventory_space": "1" if row.use_inventory_space else "0",
        "use_survival_needs": "1" if row.use_survival_needs else "0",
        **extra,
    }


def _options_events(app: FastAPI, campaign_id: int) -> list[object]:
    with Session(app.state.engine) as db:
        return [
            decode(row) for row in events_since(db, campaign_id) if row.kind == "campaign.options"
        ]


def _respec_fields(page: str) -> dict[str, str]:
    """The respec form as a player who picks the first card and first pick would send it."""
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(_picks(page))
    return fields


def _post_respec(client: TestClient, campaign_id: int, name: str, page: str, **fields: str):
    token = remember_csrf_token(client, page)
    return client.post(
        f"/campaigns/{campaign_id}/respec",
        data={"csrf_token": token, "character": name, **fields},
        follow_redirects=False,
    )


# The campaign's ruleset options in settings.


def test_the_settings_page_shows_the_rule_sets_campaign_options(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    omen = open_campaign(client.app, campaign_id).options["omen"]

    page = client.get(f"/campaigns/{campaign_id}").text

    assert '<h4 class="settings-subheading">Campaign options</h4>' in page
    assert 'name="campaign-omen"' in page
    assert f'<option value="{omen}" selected' in page
    assert 'name="setting"' in page
    assert 'name="table_genres_present" value="1"' in page
    assert 'name="table_tones_present" value="1"' in page
    assert "Let players respec their characters" in page


def test_the_owner_changes_the_campaign_options_brief_and_genres(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    current_omen = open_campaign(client.app, campaign_id).options["omen"]
    other_omen = next(
        option.value for option in CAIRN.campaign.tables["omen"] if option.value != current_omen
    )
    genre = client.app.state.content.available_table_genres[0]
    tone = client.app.state.content.rule_sets["cairn2e"].table_tones[0]

    response = _post_settings(
        client,
        campaign_id,
        **_settings_form(
            client,
            campaign_id,
            **{
                "campaign-omen": other_omen,
                "setting": "A drowned country of bell towers.",
                "table_genres_present": "1",
                "table_genres": genre,
                "table_tones_present": "1",
                "table_tones": tone,
            },
        ),
    )

    assert response.status_code == 303
    state = open_campaign(client.app, campaign_id)
    assert state.options["omen"] == other_omen
    row = _row(client.app, campaign_id)
    assert row.setting == "A drowned country of bell towers."
    assert json.loads(row.table_genres) == [genre]
    assert json.loads(row.table_tones) == [tone]
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "A drowned country of bell towers." in page


def test_saving_the_options_again_records_nothing_new(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    omen = open_campaign(client.app, campaign_id).options["omen"]
    saved = _settings_form(client, campaign_id, **{"campaign-omen": omen})
    assert _post_settings(client, campaign_id, **saved).status_code == 303
    assert len(_options_events(client.app, campaign_id)) == 1

    assert _post_settings(client, campaign_id, **saved).status_code == 303
    assert len(_options_events(client.app, campaign_id)) == 1


def test_a_settings_save_without_the_options_leaves_them_alone(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    before = open_campaign(client.app, campaign_id).options

    response = _post_settings(client, campaign_id, **_settings_form(client, campaign_id))

    assert response.status_code == 303
    assert open_campaign(client.app, campaign_id).options == before
    assert len(_options_events(client.app, campaign_id)) == 1


# The respec option in creation and settings.


def test_the_creation_page_offers_the_respec_option(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="allow_respec"' in page
    assert "Let players respec their characters" in page
    assert _row(client.app, campaign_id).allow_respec is False

    fields = _complete_form(client, campaign_id)
    response = client.post(
        f"/campaigns/{campaign_id}/creation",
        data={
            "csrf_token": remember_csrf_token(client, page),
            "name": PC,
            "pronouns": "she/her",
            "use_inventory_space": "1",
            "use_survival_needs": "1",
            "allow_respec": "1",
            **fields,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _row(client.app, campaign_id).allow_respec is True


def test_the_respec_switch_is_off_until_the_owner_turns_it_on(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    assert _row(client.app, campaign_id).allow_respec is False
    assert (
        f'action="/campaigns/{campaign_id}/respec/start"'
        not in client.get(f"/campaigns/{campaign_id}").text
    )

    response = _post_settings(
        client, campaign_id, **_settings_form(client, campaign_id, allow_respec="1")
    )

    assert response.status_code == 303
    assert _row(client.app, campaign_id).allow_respec is True
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f'action="/campaigns/{campaign_id}/respec/start"' in page


def test_the_character_panel_and_the_characters_page_offer_the_details_link(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert page.count(f'data-edit-character="{PC}"') == 2
    assert 'data-edit-from="play"' in page
    assert 'data-edit-from="characters"' in page


# A player's own words for a flavor field.


def test_a_flavor_field_takes_the_players_own_words(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="field-physique-freeform"' in page

    fields = _complete_form(client, campaign_id)
    fields["field-physique-freeform"] = "Wiry, with a sailor's rolling gait."
    response = client.post(
        f"/campaigns/{campaign_id}/creation",
        data={
            "csrf_token": remember_csrf_token(client, page),
            "name": PC,
            "pronouns": "she/her",
            "use_inventory_space": "1",
            "use_survival_needs": "1",
            **fields,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        open_campaign(client.app, campaign_id).pc().fields["physique"]
        == "Wiry, with a sailor's rolling gait."
    )


def test_every_worlds_without_number_flavor_field_offers_a_write_in(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])

    page = client.get(f"/campaigns/{campaign_id}").text
    for field_id in ("look", "personality", "accent", "quirk"):
        assert f'name="field-{field_id}-freeform"' in page
    # The free-text flavor fields are already freeform.
    for field_id in ("goal", "appearance", "biography"):
        assert f'name="field-{field_id}"' in page


# The details a player may change on a character.


def test_a_player_rewrites_their_own_characters_flavor(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)

    page = client.get(f"/campaigns/{campaign_id}/character/edit?character={PC}").text
    assert 'name="field-physique"' in page
    assert 'name="field-physique-freeform"' in page
    assert 'name="field-appearance"' in page

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/character/fields",
        data={
            "csrf_token": token,
            "character": PC,
            "from": "play",
            "field-physique": next(iter(CAIRN.tables["physique"])).value,
            "field-physique-freeform": "Broad-shouldered and weather-beaten.",
            "field-appearance": "A patched green coat and a rope belt.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("#view-play")
    character = character_named(client.app, campaign_id, PC)
    assert character.fields["physique"] == "Broad-shouldered and weather-beaten."
    assert character.fields["appearance"] == "A patched green coat and a rope belt."
    with Session(client.app.state.engine) as db:
        events = [
            decode(row)
            for row in events_since(db, campaign_id)
            if row.kind == "character.field_set"
        ]
    assert [event.label for event in events] == ["Physique", "Appearance"]
    assert all(isinstance(event, CharacterFieldSet) for event in events)


def test_a_hirelings_mechanical_fields_are_the_players_but_its_flavor_is_the_gms(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    place_pc(client, campaign_id)
    tools = tools_for(client.app, campaign_id)
    tools.hire_hireling(HIRELING, PC, 2, "A hired hand, good with a rope.")
    before = character_named(client.app, campaign_id, HIRELING)
    flavor = {key: value for key, value in before.fields.items() if key != "post"}

    page = client.get(f"/campaigns/{campaign_id}/character/edit?character={HIRELING}").text

    assert 'name="field-post"' in page
    assert 'name="field-appearance"' not in page
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/character/fields",
        data={"csrf_token": token, "character": HIRELING, "field-post": "Watch"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    after = character_named(client.app, campaign_id, HIRELING)
    assert after.fields["post"] == "Watch"
    assert {key: value for key, value in after.fields.items() if key != "post"} == flavor


def test_a_player_cannot_change_their_characters_mechanical_fields(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    token = csrf_token(client, f"/campaigns/{campaign_id}")

    response = client.post(
        f"/campaigns/{campaign_id}/character/fields",
        data={"csrf_token": token, "character": PC, "field-post": "Sneak"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert character_named(client.app, campaign_id, PC).fields.get("post", "") == ""


def test_another_players_character_details_stay_private(
    client_factory: Callable[[], TestClient],
):
    _, guest, campaign_id, _ = _joined_campaign(client_factory)

    assert guest.get(f"/campaigns/{campaign_id}/character/edit?character={PC}").status_code == 404


# Respecs.


def test_a_respec_needs_the_campaign_option(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_ready(client_factory)

    response = client.get(f"/campaigns/{campaign_id}?respec={PC}")

    assert response.status_code == 200
    assert "does not allow respeccing" in response.text


def test_a_respec_waits_for_the_gms_reply(client_factory: Callable[[], TestClient]):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client)
    create_character_direct(client, campaign_id, name=PC)
    _enable_respec(client.app, campaign_id)

    response = client.get(f"/campaigns/{campaign_id}?respec={PC}")

    assert response.status_code == 200
    assert "a respec waits until the GM" in response.text
    assert "reply is finished" in response.text


def test_only_the_characters_player_can_respec_it(
    client_factory: Callable[[], TestClient],
):
    _, guest, campaign_id, _ = _joined_campaign(client_factory)
    _enable_respec(guest.app, campaign_id)

    response = guest.get(f"/campaigns/{campaign_id}?respec={PC}")

    assert response.status_code == 200
    assert "only a character you play can be respecced" in response.text


def test_a_respec_rebuilds_the_sheet_and_keeps_the_characters_identity(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    _enable_respec(client.app, campaign_id)
    before = character_named(client.app, campaign_id, PC)
    identity = dict(before.fields)

    with pinned_stats(RESPEC_STATS):
        page = client.get(f"/campaigns/{campaign_id}?respec={PC}").text
    assert f"Rebuild {PC}" in page
    assert 'name="name"' not in page
    assert 'name="field-physique"' not in page
    assert 'name="field-appearance"' not in page
    fields = _respec_fields(page)

    response = _post_respec(client, campaign_id, PC, page, **fields)

    assert response.status_code == 303
    after = character_named(client.app, campaign_id, PC)
    assert after.name == before.name
    assert after.pronouns == before.pronouns
    assert after.description == before.description
    assert after.items == before.items
    assert after.fields == identity
    assert after.stats == RESPEC_STATS
    with Session(client.app.state.engine) as db:
        events = [
            decode(row)
            for row in events_since(db, campaign_id)
            if row.kind == "character.respecced"
        ]
    assert len(events) == 1
    assert isinstance(events[0], CharacterRespecced)


def test_the_respec_form_can_roll_the_stats_again(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    _enable_respec(client.app, campaign_id)
    page = client.get(f"/campaigns/{campaign_id}?respec={PC}").text
    token = remember_csrf_token(client, page)

    with pinned_stats(RESPEC_STATS):
        response = client.post(
            f"/campaigns/{campaign_id}/respec/reroll",
            data={"csrf_token": token, "character": PC},
        )

    assert response.status_code == 200
    assert _rolled_stats(response.text) == RESPEC_STATS


def test_the_respec_form_rolls_the_stats_in_place_for_the_script(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    _enable_respec(client.app, campaign_id)
    page = client.get(f"/campaigns/{campaign_id}?respec={PC}").text
    token = remember_csrf_token(client, page)

    with pinned_stats(RESPEC_STATS):
        response = client.post(
            f"/campaigns/{campaign_id}/respec/reroll",
            data={"csrf_token": token, "character": PC},
            headers={"accept": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {"stats": RESPEC_STATS}
    # The in-place roll is kept for the rebuild, so the player submits the values they saw.
    page = client.get(f"/campaigns/{campaign_id}?respec={PC}").text
    assert _rolled_stats(page) == RESPEC_STATS


def test_a_respec_reroll_names_the_character_it_is_for(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    _enable_respec(client.app, campaign_id)
    page = client.get(f"/campaigns/{campaign_id}?respec={PC}").text
    token = remember_csrf_token(client, page)

    response = client.post(
        f"/campaigns/{campaign_id}/respec/reroll",
        data={"csrf_token": token},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "choose a character to respec"}


def test_a_hireling_respec_keeps_its_flavor_and_rebuilds_its_sheet(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    place_pc(client, campaign_id)
    tools = tools_for(client.app, campaign_id)
    tools.hire_hireling(HIRELING, PC, 2, "A hired hand, good with a rope.")
    before = character_named(client.app, campaign_id, HIRELING)
    _enable_respec(client.app, campaign_id)

    page = client.get(f"/campaigns/{campaign_id}?respec={HIRELING}").text
    fields = _respec_fields(page)
    response = _post_respec(client, campaign_id, HIRELING, page, **fields)

    assert response.status_code == 303
    after = character_named(client.app, campaign_id, HIRELING)
    assert after.name == before.name
    assert after.hireling is True
    assert after.employer == before.employer
    assert after.description == before.description
    assert after.personality == before.personality
    assert after.fields == before.fields


def test_a_respec_refunds_levels_and_the_player_takes_them_again(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior", start_level=2)
    client = _signed_in(client_factory)
    assert _submit_advancement(client, campaign_id, {}).status_code == 303
    assert _counter(app, campaign_id, "level") == 2
    close_opening(client, campaign_id)
    _enable_respec(app, campaign_id)
    assert _row(app, campaign_id).start_level == 2
    assert _row(app, campaign_id).follow_owner_level is True

    page = client.get(f"/campaigns/{campaign_id}?respec={NAME}").text
    fields = _respec_fields(page)
    fields["template-class"] = "warrior"
    fields.update(CLASS_PICKS["warrior"])
    fields["pick-skill-class_free_skill-0"] = "notice"
    fields["template-tradition"] = "no_tradition"

    response = _post_respec(client, campaign_id, NAME, page, **fields)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == (
        f"/campaigns/{campaign_id}/advancement?character={NAME}"
    )
    assert _counter(app, campaign_id, "level") == 1
    assert _counter(app, campaign_id, "xp") == FAST_XP[1]

    assert _submit_advancement(client, campaign_id, {}).status_code == 303
    assert _counter(app, campaign_id, "level") == 2


# The option travels with an export.


def test_an_exported_campaign_carries_the_respec_option(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_ready(client_factory)
    _enable_respec(client.app, campaign_id)
    app = client.app
    owner_id = _row(app, campaign_id).owner_id

    _, payload = campaign_files.export_campaign(app.state.engine, owner_id, campaign_id)
    assert json.loads(payload)["campaign"]["allow_respec"] is True

    imported = campaign_files.import_campaign(
        app.state.engine,
        app.state.content,
        app.state.catalog.model_ids(),
        app.state.catalog.default_model_id(),
        owner_id,
        payload,
    )
    assert imported.campaign.allow_respec is True

    # A file exported before the option existed keeps respecs off.
    document = json.loads(payload)
    del document["campaign"]["allow_respec"]
    older = campaign_files.import_campaign(
        app.state.engine,
        app.state.content,
        app.state.catalog.model_ids(),
        app.state.catalog.default_model_id(),
        owner_id,
        json.dumps(document).encode(),
    )
    assert older.campaign.allow_respec is False


@pytest.mark.parametrize("value", ["maybe"])
def test_an_imported_respec_option_must_be_true_or_false(
    client_factory: Callable[[], TestClient], value: str
):
    client, campaign_id = _cairn_ready(client_factory)
    app = client.app
    owner_id = _row(app, campaign_id).owner_id
    _, payload = campaign_files.export_campaign(app.state.engine, owner_id, campaign_id)
    document = json.loads(payload)
    document["campaign"]["allow_respec"] = value

    with pytest.raises(InvalidInput, match="allow_respec must be true or false"):
        campaign_files.import_campaign(
            app.state.engine,
            app.state.content,
            app.state.catalog.model_ids(),
            app.state.catalog.default_model_id(),
            owner_id,
            json.dumps(document).encode(),
        )
