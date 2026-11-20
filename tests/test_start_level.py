"""The starting level is a campaign option for rule sets with levels.

The campaign sets it at creation, defaulting to 1, and every new character begins there. By
default it follows the campaign owner's character level, so a character joining later begins at
the table's level. The normal advancement pages then take the player through every level from 2
up, so each level's automatic gains and picks are still the player's own choices.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign
from cygnusloom.rules import wwn
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.events import CharacterDied, record
from tests.conftest import content_library, csrf_token
from tests.factories import open_campaign
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _cairn_campaign
from tests.test_wwn_advancement import (
    NAME,
    WWN,
    _campaign,
    _counter,
    _creation_fields,
    _ctx,
    _world_tools,
)

CAIRN = content_library().rule_sets["cairn2e"]
LEVELS = WWN.level_ups["level"]
# The creation form's first XP option, the fast track, needs this much experience to reach
# each level; index 1 is the experience for level 2, so the index is the previous level.
FAST_XP = LEVELS.tracks["fast"]


def _campaign_row(app, campaign_id: int) -> Campaign:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        db.expunge(campaign)
        return campaign


def _page(client: TestClient, campaign_id: int) -> str:
    return client.get(f"/campaigns/{campaign_id}/advancement").text


def _submit(client: TestClient, campaign_id: int, fields: dict[str, str]):
    token = csrf_token(client, f"/campaigns/{campaign_id}/advancement")
    return client.post(
        f"/campaigns/{campaign_id}/advancement",
        data={"csrf_token": token, "character": NAME, **fields},
        follow_redirects=False,
    )


def _wwn_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A WWN campaign whose options and character are still waiting."""
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    return client, int(response.headers["location"].rsplit("/", 1)[1])


def test_start_level_options_follow_the_level_rules() -> None:
    assert level_up_service.start_level_options(WWN) == tuple(range(1, 11))
    assert level_up_service.start_level_options(CAIRN) == ()


def _settings_fields(app, campaign_id: int) -> dict[str, str]:
    campaign = _campaign_row(app, campaign_id)
    return {
        "model": campaign.model,
        "label": campaign.name,
        "use_inventory_space": "1",
        "use_survival_needs": "1",
    }


def _post_settings(client: TestClient, campaign_id: int, **data: str):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/settings",
        data={"csrf_token": token, **data},
        follow_redirects=False,
    )


def _reset(client: TestClient, campaign_id: int):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/reset",
        data={"csrf_token": token, "confirm": "reset"},
        follow_redirects=False,
    )


def test_the_creation_page_offers_every_starting_level_of_a_ruleset_with_levels(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _wwn_campaign(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text

    select = re.search(r'<select name="start_level">(.*?)</select>', page, re.DOTALL)
    assert select is not None
    assert re.findall(r'<option value="(\d+)"', select.group(1)) == [
        str(level) for level in range(1, 11)
    ]
    assert "Level 1" in select.group(1)
    assert "Level 10" in select.group(1)
    # Both campaign options are set by default: level 1, following the owner's character.
    assert re.search(r'<option value="1" selected>Level 1</option>', select.group(1))
    assert re.search(r'name="follow_owner_level" value="1" checked', page)


def test_a_ruleset_without_levels_offers_no_starting_level(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _cairn_campaign(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'class="creation-form"' in page
    assert 'name="start_level"' not in page
    assert 'name="follow_owner_level"' not in page


def test_starting_at_level_three_grants_the_experience_and_takes_two_level_ups(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior", start_level=3)
    client = _signed_in(client_factory)

    # The choice is the campaign's, and the character is still level 1; the experience for
    # level 3 makes levels 2 and 3 ready.
    assert _campaign_row(app, campaign_id).start_level == 3
    assert _campaign_row(app, campaign_id).follow_owner_level
    assert _counter(app, campaign_id, "level") == 1
    assert _counter(app, campaign_id, "xp") == FAST_XP[2]
    assert "Level 2" in _page(client, campaign_id)

    assert _submit(client, campaign_id, {}).status_code == 303
    assert _counter(app, campaign_id, "level") == 2
    # Following the owner's character keeps the campaign option current with each level.
    assert _campaign_row(app, campaign_id).start_level == 2
    assert "Level 3" in _page(client, campaign_id)

    assert _submit(client, campaign_id, {}).status_code == 303
    assert _counter(app, campaign_id, "level") == 3
    assert _campaign_row(app, campaign_id).start_level == 3
    assert _counter(app, campaign_id, "xp") == FAST_XP[2]
    assert _counter(app, campaign_id, "skill_points") > 0
    assert _counter(app, campaign_id, "focus_picks") == 1

    # The picks from both levels stayed banked, so the normal picks page offers them.
    assert "Spend picks" in _page(client, campaign_id)
    response = _submit(client, campaign_id, {})
    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}/advancement?character={NAME}"


@pytest.mark.parametrize(
    ("start_level", "message"),
    [
        ("11", "starting level must be from 1 to 10"),
        ("0", "starting level must be from 1 to 10"),
        ("three", "starting level must be a whole number"),
    ],
)
def test_a_starting_level_the_rule_set_cannot_grant_is_refused(
    client_factory: Callable[[], TestClient], start_level: str, message: str
) -> None:
    client, campaign_id = _wwn_campaign(client_factory)
    fields = _creation_fields(client, campaign_id, class_id="warrior")

    response = _create_character(
        client,
        campaign_id,
        name=NAME,
        fields={**fields, "start_level": start_level},
    )

    assert response.status_code == 400
    assert message in response.text
    assert not open_campaign(client.app, campaign_id).pcs()


def test_a_reset_keeps_the_starting_experience_of_the_starting_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior", start_level=3)
    client = _signed_in(client_factory)
    assert _counter(app, campaign_id, "xp") == FAST_XP[2]

    assert _reset(client, campaign_id).status_code == 303

    # Experience gained in play is gone, but the starting level's experience is still the
    # party's, exactly as it was when the party was built.
    assert _counter(app, campaign_id, "xp") == FAST_XP[2]
    assert _counter(app, campaign_id, "level") == 1
    assert "Level 2" in _page(client, campaign_id)

    # Resetting again does not hand out the starting experience twice.
    assert _reset(client, campaign_id).status_code == 303
    assert _counter(app, campaign_id, "xp") == FAST_XP[2]


def test_a_reset_restores_a_starting_level_the_log_never_granted(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    # A log from before the starting level was recorded during setup: the campaign chose level 2
    # and the character never received the experience that goes with it.
    with Session(app.state.engine) as db:
        row = db.get(Campaign, campaign_id)
        assert row is not None
        row.start_level = 2
        row.follow_owner_level = False
        db.add(row)
        db.commit()
    assert _counter(app, campaign_id, "xp") == 0

    assert _reset(client, campaign_id).status_code == 303

    assert _counter(app, campaign_id, "xp") == FAST_XP[1]
    assert _counter(app, campaign_id, "level") == 1
    assert "Level 2" in _page(client, campaign_id)


def test_turning_following_off_keeps_a_fixed_starting_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior", start_level=3)
    client = _signed_in(client_factory)
    settings = _settings_fields(app, campaign_id)

    response = _post_settings(
        client, campaign_id, **settings, follow_owner_level="0", start_level="4"
    )

    assert response.status_code == 303
    row = _campaign_row(app, campaign_id)
    assert not row.follow_owner_level
    assert row.start_level == 4
    assert re.search(
        r'<option value="4" selected>Level 4</option>', client.get(f"/campaigns/{campaign_id}").text
    )

    response = _post_settings(
        client, campaign_id, **settings, follow_owner_level="0", start_level="11"
    )

    assert response.status_code == 400
    assert "starting level must be from 1 to 10" in response.text
    assert _campaign_row(app, campaign_id).start_level == 4

    # A level taken while following is off leaves the fixed level alone.
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, FAST_XP[1], "a session", ctx)
    assert _submit(client, campaign_id, {}).status_code == 303
    assert _counter(app, campaign_id, "level") == 2
    assert _campaign_row(app, campaign_id).start_level == 4

    # Turning it back on follows the owner's character straight away.
    response = _post_settings(client, campaign_id, **settings, follow_owner_level="1")
    assert response.status_code == 303
    row = _campaign_row(app, campaign_id)
    assert row.follow_owner_level
    assert row.start_level == 2


def test_a_replacement_character_starts_at_the_owners_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior", start_level=3)
    client = _signed_in(client_factory)
    # Finish the owner's starting levels first; the replacement follows their current level.
    assert _submit(client, campaign_id, {}).status_code == 303
    assert _submit(client, campaign_id, {}).status_code == 303
    assert _counter(app, campaign_id, "level") == 3
    with Session(app.state.engine) as db:
        record(db, campaign_id, CharacterDied(name=NAME))
        db.commit()
    fields = _creation_fields(client, campaign_id, class_id="warrior")

    response = _create_character(client, campaign_id, name="Bran", fields=fields)

    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}/advancement"
    replacement = open_campaign(app, campaign_id).characters["bran"]
    assert replacement.counters.get("level", WWN.counters["level"].initial) == 1
    assert replacement.counters.get("xp", WWN.counters["xp"].initial) == FAST_XP[2]
