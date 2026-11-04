"""The player's advancement page: level-up choices and pick spending."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from fastapi.testclient import TestClient

from cygnusloom.config import Settings
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.rules import wwn
from tests.conftest import csrf_token, make_app
from tests.test_campaigns import _signed_in
from tests.test_wwn_advancement import (
    NAME,
    WWN,
    _counter,
    _ctx,
    _pc,
    _world_tools,
)
from tests.test_wwn_advancement import (
    _campaign as _wwn_campaign,
)


def _page(client: TestClient, campaign_id: int) -> str:
    return client.get(f"/campaigns/{campaign_id}/advancement").text


def test_the_level_up_tracks_match_the_progression_data() -> None:
    progression = WWN.rule_data["progression"]
    level_up = WWN.level_ups["level"]

    assert level_up.tracks["fast"] == progression.xp_fast
    assert level_up.tracks["slow"] == progression.xp_slow


def _submit(
    client: TestClient,
    campaign_id: int,
    fields: dict[str, str],
):
    token = csrf_token(client, f"/campaigns/{campaign_id}/advancement")
    return client.post(
        f"/campaigns/{campaign_id}/advancement",
        data={"csrf_token": token, **fields},
        follow_redirects=False,
    )


def test_a_ready_level_shows_its_automatic_gains_and_picks(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    page = _page(client, campaign_id)

    assert "Level 2" in page
    assert "Skill points" in page
    assert "Focus" in page
    assert "level up to 2" in page.lower()


def test_a_dual_partial_level_page_shows_the_combined_chart(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="healer", class2="warrior_partial_2")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    page = _page(client, campaign_id)

    assert "Attack bonus: 2." in page
    assert "Skill points: 3." in page
    assert "Focus pick: 1." in page
    assert "Art pick: 1." in page


def test_level_up_applies_the_level_and_the_players_choices(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)
    before = _pc(app, campaign_id).skills.get("sneak", -1)

    response = _submit(
        client,
        campaign_id,
        {
            "skill-skills-sneak": "0",
            "pick-focus-0": "focus_authority",
        },
    )

    assert response.status_code == 303, response.text
    character = _pc(app, campaign_id)
    assert _counter(app, campaign_id, "level") == 2
    assert character.skills.get("sneak", -1) == 0
    assert character.skills.get("sneak", -1) == before + 1
    assert "focus_authority" in character.features
    assert _counter(app, campaign_id, "skill_points") == 2
    assert _counter(app, campaign_id, "focus_picks") == 0


def test_a_level_up_waits_for_the_player_instead_of_the_gm(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    result = wwn.level_up(NAME, ctx)

    assert result["pending"] is True
    assert _counter(app, campaign_id, "level") == 1


def test_a_message_is_refused_while_a_level_is_due(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": "Onward."},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "level up" in response.text.lower()


def test_banked_picks_can_be_spent_without_a_new_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.adjust_counter(NAME, "skill_points", 1)

    page = _page(client, campaign_id)
    assert "Spend picks" in page

    response = _submit(client, campaign_id, {"skill-skills-sneak": "0"})

    assert response.status_code == 303
    assert _pc(app, campaign_id).skills.get("sneak", -1) == 0


def test_a_bad_skill_choice_writes_no_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    response = _submit(client, campaign_id, {"skill-skills-sneak": "4"})

    assert response.status_code == 400
    assert "cannot be raised" in response.text or "needs character level" in response.text
    assert _counter(app, campaign_id, "level") == 1


def test_a_mage_levels_with_an_art_and_two_spells(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="mage", tradition="high_mage")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)
    character = _pc(app, campaign_id)
    art = next(
        feature.id
        for feature in WWN.character.features.values()
        if feature.feature_type == "feat"
        and feature.tag == "high"
        and feature.rank == 0
        and feature.id not in character.features
    )
    spells = [
        feature.id
        for feature in WWN.character.features.values()
        if feature.feature_type == "spell"
        and feature.tag == "high"
        and feature.rank == 1
        and feature.id not in character.features
    ][:2]

    response = _submit(
        client,
        campaign_id,
        {
            "pick-art-0": art,
            "pick-spells-0": spells[0],
            "pick-spells-1": spells[1],
        },
    )

    assert response.status_code == 303, response.text
    character = _pc(app, campaign_id)
    assert art in character.features
    assert spells[0] in character.features
    assert spells[1] in character.features
    assert _counter(app, campaign_id, "art_picks") == 0
    assert _counter(app, campaign_id, "spell_picks") == 0


def test_an_attribute_boost_spends_skill_points(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)
    before = _pc(app, campaign_id).stats["str"]

    response = _submit(client, campaign_id, {"stat-attributes-str": "1"})

    assert response.status_code == 303, response.text
    character = _pc(app, campaign_id)
    assert character.stats["str"] == before + 1
    assert _counter(app, campaign_id, "attribute_boosts") == 1
    assert _counter(app, campaign_id, "skill_points") == 2


def test_a_skill_raised_two_levels_costs_every_step(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    response = _submit(client, campaign_id, {"skill-skills-sneak": "1"})

    assert response.status_code == 303, response.text
    assert _pc(app, campaign_id).skills.get("sneak", -1) == 1
    assert _counter(app, campaign_id, "skill_points") == 0


def test_the_debug_panel_awards_experience_without_a_gm_turn(
    settings: Settings, fake: FakeProvider
) -> None:
    app = make_app(replace(settings, debug_panel=True), fake_provider=fake)
    client_factory = lambda: TestClient(app)
    _app, campaign_id = _wwn_campaign(client_factory, class_id="warrior")
    client = _signed_in(client_factory)
    token = csrf_token(client, f"/campaigns/{campaign_id}")

    response = client.post(
        f"/campaigns/{campaign_id}/debug/grant",
        data={
            "csrf_token": token,
            "name": NAME,
            "kind": "xp",
            "amount": "3",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _counter(app, campaign_id, "xp") == 3
