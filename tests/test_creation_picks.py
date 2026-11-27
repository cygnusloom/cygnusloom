"""Level-up picks that belong on the creation form instead of a separate page.

A starting spellcaster has picks as soon as the character exists. The creation form offers
them beside the other character options, randomizes them with the rest of the form, and spends
them when the character is recorded. Picks a creation form does not offer stay banked for the
normal advancement page.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.datastructures import FormData

from cygnusloom.services import campaigns as campaigns_service
from cygnusloom.services import level_up as level_up_service
from cygnusloom.web.character_form import submission_from_form

from .factories import (
    character_named,
    create_campaign_direct,
    create_character,
    creation_fields,
    rules_for,
    signed_in_client,
)


def _dnd_rules() -> Any:
    return rules_for("dnd-srd-2024")


def _dnd_campaign(client: TestClient) -> int:
    return create_campaign_direct(client, world_template="testhollow", rule_set="dnd-srd-2024")


def _wizard_fields(fields: dict[str, str], picks: dict[str, str] | None = None) -> dict[str, str]:
    """A wizard's required creation values over the stats the page rolled."""
    fields = dict(fields)
    fields.update(
        {
            "template-species": "elf",
            "template-background": "sage",
            "template-class": "wizard",
            "pick-skill-class_skills-0": "investigation",
            "pick-skill-class_skills-1": "insight",
            "feature-choice-background-asi-sage-scores-0": "int",
            "feature-choice-background-asi-sage-scores-1": "wis",
            "feature-choice-background-asi-sage-scores-2": "con",
        }
    )
    if picks:
        fields.update(picks)
    return fields


def _alerts(page: str) -> list[str]:
    """The error lines a failed creation page shows, without the whole page in test output."""
    return [
        re.sub(r"\\s+", " ", alert).strip()
        for alert in re.findall(r'<p class="form-error" role="alert">(.*?)</p>', page, re.DOTALL)
    ]


def test_a_class_pick_cannot_repeat_a_spells_feat_choice(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = _dnd_campaign(client)
    fields = _wizard_fields(
        creation_fields(client, campaign_id),
        {
            "feature-choice-feat-magic-initiate-list-0": "wizard",
            "feature-choice-feat-magic-initiate-cantrips-0": "spell-fire-bolt",
            "feature-choice-feat-magic-initiate-cantrips-1": "spell-light",
            "feature-choice-feat-magic-initiate-spell-0": "spell-magic-missile",
            "feature-choice-feat-magic-initiate-ability-0": "int",
            "creation-pick-wizard-cantrips-0": "spell-fire-bolt",
        },
    )

    response = create_character(client, campaign_id, fields=fields, start=False)

    assert response.status_code == 400
    assert "already granted by another choice" in response.text
    assert level_up_service.character_from(client.app.state.engine, campaign_id, "Ada") is None


def test_creating_a_wizard_spends_the_creation_picks(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = _dnd_campaign(client)
    fields = _wizard_fields(
        creation_fields(client, campaign_id),
        {
            "creation-pick-wizard-cantrips-0": "spell-fire-bolt",
            "creation-pick-wizard-cantrips-1": "spell-light",
            "creation-pick-wizard-cantrips-2": "spell-mage-hand",
            "creation-pick-wizard-spells-0": "spell-magic-missile",
            "creation-pick-wizard-spells-1": "spell-shield",
            "creation-pick-wizard-spells-2": "spell-sleep",
            "creation-pick-wizard-spells-3": "spell-thunderwave",
            "creation-pick-wizard-spells-4": "spell-detect-magic",
            "creation-pick-wizard-spells-5": "spell-fog-cloud",
        },
    )

    response = create_character(client, campaign_id, fields=fields, start=False)

    assert response.status_code == 303, _alerts(response.text)
    assert response.headers["location"] == f"/campaigns/{campaign_id}"
    pc = character_named(app, campaign_id, "Ada")
    for spell in (
        "spell-fire-bolt",
        "spell-light",
        "spell-mage-hand",
        "spell-magic-missile",
        "spell-shield",
        "spell-sleep",
        "spell-thunderwave",
        "spell-detect-magic",
        "spell-fog-cloud",
    ):
        assert spell in pc.features, spell
    assert pc.counters["cantrip_picks"] == 0
    assert pc.counters["spell_picks"] == 0
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    assert (
        level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Ada")
        is None
    )


def test_a_blank_creation_pick_is_rolled(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = _dnd_campaign(client)

    response = create_character(
        client,
        campaign_id,
        fields=_wizard_fields(creation_fields(client, campaign_id)),
        start=False,
    )

    assert response.status_code == 303, _alerts(response.text)
    pc = character_named(app, campaign_id, "Ada")
    assert pc.counters["cantrip_picks"] == 0
    assert pc.counters["spell_picks"] == 0
    spells = [
        feature_id
        for feature_id in pc.features
        if _dnd_rules().character.features[feature_id].feature_type == "spell"
    ]
    assert len(spells) >= 9


def test_a_creation_pick_outside_the_class_list_is_refused(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = _dnd_campaign(client)
    fields = _wizard_fields(
        creation_fields(client, campaign_id),
        {"creation-pick-wizard-spells-0": "spell-cure-wounds"},
    )

    response = create_character(client, campaign_id, fields=fields, start=False)

    assert response.status_code == 400
    assert "not one of the spells" in response.text
    assert level_up_service.character_from(client.app.state.engine, campaign_id, "Ada") is None


def test_a_starting_level_spends_creation_picks_before_the_level_pages(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = _dnd_campaign(client)
    fields = _wizard_fields(creation_fields(client, campaign_id))
    fields.update({"start_level": "2", "follow_owner_level": "0"})

    response = create_character(client, campaign_id, fields=fields, start=False)

    assert response.status_code == 303, _alerts(response.text)
    assert response.headers["location"] == f"/campaigns/{campaign_id}/advancement"
    pc = character_named(app, campaign_id, "Ada")
    # The level-1 spell picks were part of creation; the level-up page only has level 2's gains.
    assert pc.counters["cantrip_picks"] == 0
    assert pc.counters["spell_picks"] == 0
    assert pc.counters.get("level", 1) == 1


def test_respec_spends_the_creation_picks_again(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = _dnd_campaign(client)
    fields = _wizard_fields(creation_fields(client, campaign_id))
    response = create_character(client, campaign_id, fields=fields, start=False)
    assert response.status_code == 303, _alerts(response.text)
    with Session(app.state.engine) as db:
        campaign = db.get(campaigns_service.Campaign, campaign_id)
        assert campaign is not None
        campaign.allow_respec = True
        owner_id = campaign.owner_id
        db.add(campaign)
        db.commit()

    rules = _dnd_rules()
    spec = rules.character
    rebuilt = _wizard_fields(
        {
            "stat-str": "8",
            "stat-dex": "14",
            "stat-con": "13",
            "stat-int": "15",
            "stat-wis": "12",
            "stat-cha": "10",
        },
        {
            "creation-pick-wizard-cantrips-0": "spell-fire-bolt",
            "creation-pick-wizard-cantrips-1": "spell-light",
            "creation-pick-wizard-cantrips-2": "spell-mage-hand",
            "creation-pick-wizard-spells-0": "spell-magic-missile",
            "creation-pick-wizard-spells-1": "spell-shield",
            "creation-pick-wizard-spells-2": "spell-sleep",
            "creation-pick-wizard-spells-3": "spell-thunderwave",
            "creation-pick-wizard-spells-4": "spell-detect-magic",
            "creation-pick-wizard-spells-5": "spell-fog-cloud",
        },
    )
    form = FormData([("name", "Ada"), ("pronouns", "she/her"), *rebuilt.items()])
    submitted = submission_from_form(spec, form)
    rolled = {
        key.removeprefix("stat-"): int(value)
        for key, value in rebuilt.items()
        if key.startswith("stat-")
    }
    starting_level = campaigns_service.respec_character(
        app.state.engine,
        client.app.state.content,
        owner_id,
        campaign_id,
        name="Ada",
        rolled=rolled,
        submitted=submitted,
        creation_picks={
            "cantrips": ["spell-fire-bolt", "spell-light", "spell-mage-hand"],
            "spells": [
                "spell-magic-missile",
                "spell-shield",
                "spell-sleep",
                "spell-thunderwave",
                "spell-detect-magic",
                "spell-fog-cloud",
            ],
        },
        creation_form={key: value for key, value in form.items()},
    )

    assert starting_level == 1
    pc = character_named(app, campaign_id, "Ada")
    assert pc.counters["cantrip_picks"] == 0
    assert pc.counters["spell_picks"] == 0
    assert "spell-fire-bolt" in pc.features
    assert "spell-fog-cloud" in pc.features
