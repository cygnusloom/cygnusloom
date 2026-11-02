from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.character import (
    CharacterError,
    FeatPick,
    Granted,
    TableOption,
    build_character,
    check_choices,
    feature_pick_options,
    resolve,
)
from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import ContentLibrary
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import Character, NpcAdded, load_state, record
from cygnusforge.web.character_form import creation_view
from tests.conftest import content_library
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_hogwarts import TRAITS
from tests.test_wwn_combat import PC, _wwn_campaign

TAG = "3rd Year"
BASE = content_library().rule_sets["hogwarts-rpg"].character


def _tagged_spec(count: int):
    template = BASE.templates["pure-blood"]
    assert template.choose is not None
    source = next(feature for feature in BASE.features.values() if feature.feature_type == "spell")
    traits = {
        f"test-trait-{index}": replace(
            source,
            id=f"test-trait-{index}",
            name=f"Test Trait {index}",
            feature_type="trait",
            tag=TAG,
            effects=(),
        )
        for index in range(count)
    }
    return replace(
        BASE,
        features={
            **{
                ident: feature
                for ident, feature in BASE.features.items()
                if feature.feature_type != "trait"
            },
            **traits,
        },
        templates={
            **BASE.templates,
            template.id: replace(
                template, choose=replace(template.choose, count=2, feature_type="trait")
            ),
        },
    )


def _skip_spec(skip_tags: object):
    definitions = [
        Definition(
            "character_field",
            "realm",
            {"type": "character_field", "id": "realm", "name": "Realm", "table": "realms"},
        ),
        Definition(
            "feature",
            "known_trait",
            {
                "type": "feature",
                "id": "known_trait",
                "name": "Known Trait",
                "description": "Already known.",
                "feature_type": "trait",
                "tag": "known",
            },
        ),
        Definition(
            "template",
            "origin",
            {
                "type": "template",
                "id": "origin",
                "kind": "origin",
                "name": "Origin",
                "description": "An origin.",
                "choose": {
                    "count": 2,
                    "feature_type": "trait",
                    "tag_field": "realm",
                    "skip_tags": skip_tags,
                },
            },
        ),
    ]
    tables = {"realms": (TableOption(None, "known"), TableOption(None, "other"))}
    return build_character(definitions, tables, {"realms": "Realms"}, "test")


def _feat_pick_spec(count: int, picks: tuple[FeatPick, ...]):
    spec = _tagged_spec(count)
    template = spec.templates["pure-blood"]
    return replace(
        spec,
        templates={
            **spec.templates,
            template.id: replace(template, choose=None, feat_picks=picks),
        },
    )


def test_ruleset_can_explicitly_skip_tagged_feature_picks_for_a_tag():
    spec = _skip_spec(["known"])
    choose = spec.templates["origin"].choose
    assert choose is not None and choose.skip_tags == ("known",)
    submitted = {"templates": {"origin": "origin"}, "fields": {"realm": "known"}}
    check_choices(spec, submitted)

    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert not sheet.features
    view = creation_view(spec, {})
    assert view["kinds"][0]["templates"][0]["picks"]["skip_tags"] == ["known"]
    with pytest.raises(CharacterError, match="does not pick"):
        check_choices(spec, {**submitted, "features": ["known_trait"]})


@pytest.mark.parametrize("skip_tags", [None, "known", ["known", "known"], ["missing"]])
def test_invalid_skipped_tags_are_rejected_at_load(skip_tags: object):
    with pytest.raises(ContentError, match="skip_tags"):
        _skip_spec(skip_tags)


@pytest.mark.parametrize(("available", "manual"), [(0, False), (1, False), (1, True)])
def test_tagged_feature_picks_reject_an_insufficient_pool(available: int, manual: bool):
    spec = _tagged_spec(available)
    submitted = {
        "templates": {"blood": "pure-blood"},
        "fields": {"year": TAG},
        "features": ["test-trait-0"] if manual else [],
    }
    check_choices(spec, submitted)

    with pytest.raises(CharacterError, match=rf"trait.*3rd Year.*2.*{available}"):
        resolve(spec, submitted, random.Random(0), plus="sum")


def test_a_partial_tagged_feature_choice_rolls_the_remaining_slot():
    spec = _tagged_spec(2)
    submitted = {
        "templates": {"blood": "pure-blood"},
        "fields": {"year": TAG},
        "features": ["test-trait-0"],
    }
    check_choices(spec, submitted)

    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert {"test-trait-0", "test-trait-1"} <= set(sheet.features)


@pytest.mark.parametrize(("available", "manual"), [(0, []), (1, []), (1, ["test-trait-0"])])
def test_feature_pick_rejects_an_insufficient_pool(available: int, manual: list[str]):
    pick = FeatPick(id="talents", count=2, feature_type="trait", tag=TAG)
    spec = _feat_pick_spec(available, (pick,))
    submitted = {
        "templates": {"blood": "pure-blood"},
        "feature_picks": {"talents": manual},
    }
    check_choices(spec, submitted)

    with pytest.raises(CharacterError, match=r"feature pick 'talents'.*(cannot|only)"):
        resolve(spec, submitted, random.Random(0), plus="sum")


def test_a_partial_feature_pick_rolls_its_remaining_slot():
    pick = FeatPick(id="talents", count=2, feature_type="trait", tag=TAG)
    spec = _feat_pick_spec(2, (pick,))
    submitted = {
        "templates": {"blood": "pure-blood"},
        "feature_picks": {"talents": ["test-trait-0"]},
    }
    check_choices(spec, submitted)

    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1
    assert sheet.features["test-trait-1"] == 1


@pytest.mark.parametrize("later_manual", [False, True])
def test_feature_picks_reserve_options_needed_by_later_picks(later_manual: bool):
    picks = (
        FeatPick(id="early", count=1, feature_type="trait", tags=("red", "blue")),
        FeatPick(id="later", count=1, feature_type="trait", tag="red"),
    )
    spec = _feat_pick_spec(2, picks)
    spec = replace(
        spec,
        features={
            **spec.features,
            "test-trait-0": replace(spec.features["test-trait-0"], tag="red"),
            "test-trait-1": replace(spec.features["test-trait-1"], tag="blue"),
        },
    )
    submitted = {
        "templates": {"blood": "pure-blood"},
        "feature_picks": {"later": ["test-trait-0"]} if later_manual else {},
    }
    check_choices(spec, submitted)

    for seed in range(10):
        sheet = resolve(spec, submitted, random.Random(seed), plus="sum")
        assert sheet.features["test-trait-0"] == 1
        assert sheet.features["test-trait-1"] == 1


@pytest.mark.parametrize("later_manual", [False, True])
def test_tagged_and_feature_picks_do_not_consume_each_others_only_options(later_manual: bool):
    spec = _tagged_spec(3)
    template = spec.templates["pure-blood"]
    pick = FeatPick(
        id="limited",
        count=1,
        feature_type="trait",
        tag=TAG,
        exclude=("test-trait-1", "test-trait-2"),
    )
    spec = replace(
        spec,
        templates={**spec.templates, template.id: replace(template, feat_picks=(pick,))},
    )
    submitted = {
        "templates": {"blood": template.id},
        "fields": {"year": TAG},
        "feature_picks": {"limited": ["test-trait-0"]} if later_manual else {},
    }
    check_choices(spec, submitted)

    for seed in range(10):
        sheet = resolve(spec, submitted, random.Random(seed), plus="sum")
        for index in range(3):
            assert sheet.features[f"test-trait-{index}"] == 1


def test_a_later_manual_item_can_match_an_earlier_roll_when_no_alternative_exists():
    picks = (
        FeatPick(id="early", count=1, feature_type="item", tag=TAG),
        FeatPick(id="later", count=1, feature_type="item", tag=TAG),
    )
    spec = _feat_pick_spec(1, picks)
    item = replace(spec.features["test-trait-0"], feature_type="item")
    spec = replace(spec, features={**spec.features, item.id: item})
    submitted = {
        "templates": {"blood": "pure-blood"},
        "feature_picks": {"later": [item.id]},
    }
    check_choices(spec, submitted)

    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features[item.id] == 2


@pytest.mark.parametrize(
    ("advance", "has_next", "expected"),
    [
        (False, True, {"test-trait-0": 2}),
        (True, False, {"test-trait-0": 2}),
        (True, True, {"test-trait-0": 1, "test-trait-0-rank-2": 1}),
    ],
)
def test_repeated_ranked_picks_only_advance_when_configured(
    advance: bool, has_next: bool, expected: dict[str, int]
):
    picks = (
        FeatPick(id="early", count=1, feature_type="trait", tag=TAG, max_rank=1),
        FeatPick(
            id="later",
            count=1,
            feature_type="trait",
            tag=TAG,
            max_rank=1,
            advance_repeated_rank=advance,
        ),
    )
    spec = _feat_pick_spec(1, picks)
    first = replace(spec.features["test-trait-0"], rank=1)
    features = {**spec.features, first.id: first}
    if has_next:
        second = replace(first, id="test-trait-0-rank-2", rank=2)
        features[second.id] = second
    spec = replace(spec, features=features)
    submitted = {
        "templates": {"blood": "pure-blood"},
        "feature_picks": {"early": [first.id], "later": [first.id]},
    }
    check_choices(spec, submitted)
    sheet = resolve(spec, submitted, random.Random(0), plus="sum")
    assert sheet.features == expected


@pytest.mark.parametrize("conflict", ["ambiguous", "owned"])
def test_repeated_rank_cannot_advance_to_an_ambiguous_or_owned_feature(conflict: str):
    picks = (
        FeatPick(id="early", count=1, feature_type="trait", tag=TAG, max_rank=1),
        FeatPick(
            id="later",
            count=1,
            feature_type="trait",
            tag=TAG,
            max_rank=1,
            advance_repeated_rank=True,
        ),
    )
    spec = _feat_pick_spec(1, picks)
    first = replace(spec.features["test-trait-0"], rank=1)
    second = replace(first, id="rank-2", rank=2)
    features = {**spec.features, first.id: first, second.id: second}
    template = spec.templates["pure-blood"]
    if conflict == "ambiguous":
        features["other-rank-2"] = replace(second, id="other-rank-2")
        message = "more than one next rank"
    else:
        template = replace(template, features=(Granted(second.id, "1"),))
        message = "already reached its next rank"
    spec = replace(spec, features=features, templates={**spec.templates, template.id: template})
    submitted = {
        "templates": {"blood": template.id},
        "feature_picks": {"early": [first.id], "later": [first.id]},
    }
    check_choices(spec, submitted)
    with pytest.raises(CharacterError, match=message):
        resolve(spec, submitted, random.Random(0), plus="sum")


def test_rank_advancement_option_must_be_boolean():
    definitions = [
        Definition(
            "template",
            "origin",
            {
                "type": "template",
                "id": "origin",
                "kind": "origin",
                "name": "Origin",
                "description": "An origin.",
                "feat_picks": [
                    {"id": "talent", "count": 1, "tag": "talent", "advance_repeated_rank": "yes"}
                ],
            },
        )
    ]
    with pytest.raises(ContentError, match="advance_repeated_rank"):
        build_character(definitions, {}, {}, "test")


@pytest.mark.parametrize("possible", [False, True])
def test_overlapping_feature_pools_are_assigned_when_a_full_solution_exists(possible: bool):
    allowed = (
        {"test-trait-0", "test-trait-1"},
        {"test-trait-0", "test-trait-2"} if possible else {"test-trait-0", "test-trait-1"},
        {"test-trait-0", "test-trait-2"} if possible else {"test-trait-0", "test-trait-1"},
    )
    all_ids = {f"test-trait-{index}" for index in range(3)}
    picks = tuple(
        FeatPick(
            id=f"pick-{index}",
            count=1,
            feature_type="trait",
            tag=TAG,
            exclude=tuple(sorted(all_ids - options)),
        )
        for index, options in enumerate(allowed)
    )
    spec = _feat_pick_spec(3, picks)
    submitted = {"templates": {"blood": "pure-blood"}}
    check_choices(spec, submitted)

    for seed in range(20):
        if possible:
            sheet = resolve(spec, submitted, random.Random(seed), plus="sum")
            assert all_ids <= set(sheet.features)
        else:
            with pytest.raises(CharacterError, match=r"feature pick.*cannot.*distinct"):
                resolve(spec, submitted, random.Random(seed), plus="sum")


def test_templates_with_the_same_tagged_pick_can_share_already_planned_features():
    spec = _tagged_spec(2)
    first = spec.templates["pure-blood"]
    second = replace(first, id="other-origin", kind="other-origin")
    spec = replace(spec, templates={**spec.templates, second.id: second})
    submitted = {
        "templates": {"blood": first.id, second.kind: second.id},
        "fields": {"year": TAG},
    }
    check_choices(spec, submitted)

    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1
    assert sheet.features["test-trait-1"] == 1


def test_manual_tagged_picks_can_belong_to_different_template_fields():
    spec = _tagged_spec(2)
    first = spec.templates["pure-blood"]
    choose = first.choose
    assert choose is not None
    choose = replace(choose, count=1)
    first = replace(first, choose=choose)
    second = replace(
        first,
        id="other-origin",
        kind="other-origin",
        choose=replace(choose, tag_field="other_year"),
    )
    earlier = replace(spec.features["test-trait-1"], tag="2nd Year")
    other_field = replace(spec.fields["year"], id="other_year")
    spec = replace(
        spec,
        features={**spec.features, earlier.id: earlier},
        fields={**spec.fields, other_field.id: other_field},
        templates={**spec.templates, first.id: first, second.id: second},
    )
    submitted = {
        "templates": {"blood": first.id, second.kind: second.id},
        "fields": {"year": TAG, "other_year": "2nd Year"},
        "features": ["test-trait-0", earlier.id],
    }

    check_choices(spec, submitted)
    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1
    assert sheet.features[earlier.id] == 1


def test_an_unselected_tag_field_does_not_block_another_groups_manual_pick():
    spec = _tagged_spec(2)
    first = spec.templates["pure-blood"]
    choose = first.choose
    assert choose is not None
    choose = replace(choose, count=1)
    first = replace(first, choose=choose)
    second = replace(
        first,
        id="other-origin",
        kind="other-origin",
        choose=replace(choose, tag_field="other_year"),
    )
    earlier = replace(spec.features["test-trait-1"], tag="2nd Year")
    other_field = replace(spec.fields["year"], id="other_year", table="other_year")
    spec = replace(
        spec,
        features={**spec.features, earlier.id: earlier},
        fields={**spec.fields, other_field.id: other_field},
        tables={**spec.tables, "other_year": (TableOption(None, "2nd Year"),)},
        templates={**spec.templates, first.id: first, second.id: second},
    )
    submitted = {
        "templates": {"blood": first.id, second.kind: second.id},
        "fields": {"year": TAG},
        "features": ["test-trait-0"],
    }

    check_choices(spec, submitted)
    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1
    assert sheet.features[earlier.id] == 1
    with pytest.raises(CharacterError, match="give 'other_year'"):
        check_choices(spec, {**submitted, "features": [earlier.id]})


def test_manual_picks_use_the_largest_same_tag_requirement():
    spec = _tagged_spec(2)
    first = spec.templates["pure-blood"]
    assert first.choose is not None
    second = replace(
        first,
        id="other-origin",
        kind="other-origin",
        choose=replace(first.choose, count=1),
    )
    spec = replace(spec, templates={**spec.templates, second.id: second})
    submitted = {
        "templates": {"blood": first.id, second.kind: second.id},
        "fields": {"year": TAG},
        "features": ["test-trait-0", "test-trait-1"],
    }

    check_choices(spec, submitted)
    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1
    assert sheet.features["test-trait-1"] == 1


def test_a_skipped_tag_does_not_forbid_another_templates_pick():
    spec = _tagged_spec(1)
    first = spec.templates["pure-blood"]
    assert first.choose is not None
    second = replace(
        first,
        id="other-origin",
        kind="other-origin",
        choose=replace(first.choose, count=1),
    )
    first = replace(first, choose=replace(first.choose, skip_tags=(TAG,)))
    spec = replace(spec, templates={**spec.templates, first.id: first, second.id: second})
    submitted = {
        "templates": {"blood": first.id, second.kind: second.id},
        "fields": {"year": TAG},
        "features": ["test-trait-0"],
    }

    check_choices(spec, submitted)
    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1


def test_bundled_tagged_feature_pools_fill_every_required_pick():
    for rule_set in ContentLibrary.load().rule_sets.values():
        spec = rule_set.character
        for template in spec.templates.values():
            choose = template.choose
            if choose is None:
                continue
            field = spec.fields[choose.tag_field]
            assert field.table is not None
            for option in spec.tables[field.table]:
                if option.value in choose.skip_tags:
                    continue
                available = sum(
                    feature.feature_type == choose.feature_type and feature.tag == option.value
                    for feature in spec.features.values()
                )
                assert available >= choose.count, (
                    f"{rule_set.id}: {template.id} requires {choose.count} "
                    f"{choose.feature_type}s for {option.value!r}, but has {available}"
                )


def test_bundled_feature_pools_fill_each_template_pick():
    for rule_set in ContentLibrary.load().rule_sets.values():
        spec = rule_set.character
        for template in spec.templates.values():
            granted = {feature.feature for feature in template.features}
            for pick in template.feat_picks:
                available = sum(
                    feature.id not in granted for feature in feature_pick_options(spec, pick)
                )
                assert available >= pick.count, (
                    f"{rule_set.id}: {template.id} requires {pick.count} options for "
                    f"{pick.id!r}, but has {available}"
                )


def test_a_granted_feature_from_another_tag_does_not_fill_a_pick():
    spec = _tagged_spec(1)
    template = spec.templates["pure-blood"]
    foreign = replace(spec.features["test-trait-0"], id="foreign-trait", tag="2nd Year")
    spec = replace(
        spec,
        features={**spec.features, foreign.id: foreign},
        templates={
            **spec.templates,
            template.id: replace(
                template, features=(*template.features, Granted(feature=foreign.id, quantity="1"))
            ),
        },
    )
    submitted = {"templates": {"blood": template.id}, "fields": {"year": TAG}}

    with pytest.raises(CharacterError, match=r"trait.*3rd Year.*2.*1"):
        resolve(spec, submitted, random.Random(0), plus="sum")


def test_a_zero_quantity_grant_does_not_fill_a_tagged_pick():
    spec = _tagged_spec(2)
    template = spec.templates["pure-blood"]
    spec = replace(
        spec,
        templates={
            **spec.templates,
            template.id: replace(
                template,
                features=(*template.features, Granted(feature="test-trait-0", quantity="1d1-1")),
            ),
        },
    )
    submitted = {"templates": {"blood": template.id}, "fields": {"year": TAG}}

    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.features["test-trait-0"] == 1
    assert sheet.features["test-trait-1"] == 1


def test_a_negative_grant_quantity_is_rejected():
    spec = _tagged_spec(2)
    template = spec.templates["pure-blood"]
    spec = replace(
        spec,
        templates={
            **spec.templates,
            template.id: replace(
                template,
                features=(*template.features, Granted(feature="test-trait-0", quantity="1d1-2")),
            ),
        },
    )
    submitted = {"templates": {"blood": template.id}, "fields": {"year": TAG}}

    with pytest.raises(CharacterError, match=r"test-trait-0.*negative quantity"):
        resolve(spec, submitted, random.Random(0), plus="sum")


@pytest.mark.parametrize("pick_kind", ["tagged", "feature"])
def test_sparse_features_are_reported_on_the_creation_form(
    client_factory: Callable[[], TestClient],
    pick_kind: str,
):
    client = _signed_in(client_factory)
    app = client.app
    assert isinstance(app, FastAPI)
    library = app.state.content
    rule_set = library.rule_sets["hogwarts-rpg"]
    spec = (
        _tagged_spec(1)
        if pick_kind == "tagged"
        else _feat_pick_spec(1, (FeatPick(id="talents", count=2, feature_type="trait", tag=TAG),))
    )
    app.state.content = replace(
        library,
        rule_sets={
            **library.rule_sets,
            rule_set.id: replace(rule_set, character=spec),
        },
    )
    response = _new_campaign(client, world_template="hogwarts", rule_set=rule_set.id)
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    fields = {**TRAITS, "template-blood": "pure-blood", "field-year": TAG}

    created = _create_character(client, campaign_id, name="Wren", fields=fields)

    assert created.status_code == 400
    assert ("trait" if pick_kind == "tagged" else "talents") in created.text
    assert "only 1" in created.text
    with Session(app.state.engine) as db:
        assert load_state(db, campaign_id).pc() is None


@pytest.mark.parametrize("pick_kind", ["tagged", "feature"])
def test_sparse_features_are_reported_for_npcs_and_hirelings(client_factory, pick_kind: str):
    _, campaign_id, original_tools = _wwn_campaign(client_factory)
    spec = (
        _tagged_spec(1)
        if pick_kind == "tagged"
        else _feat_pick_spec(1, (FeatPick(id="talents", count=2, feature_type="trait", tag=TAG),))
    )
    tools = replace(original_tools, character=spec)
    tools.create_location("Yard", "A quiet yard.")
    tools.move_actor(PC, "Yard")
    character = {"templates": {"blood": "pure-blood"}, "fields": {"year": TAG}}
    error = r"trait.*3rd Year.*2.*1" if pick_kind == "tagged" else r"talents.*2.*1"

    with pytest.raises(InvalidInput, match=error):
        tools.create_npc("Scout", "Yard", "A scout.", character=character)
    with pytest.raises(InvalidInput, match=error):
        tools.hire_hireling("Guide", PC, 1, "A guide.", character=character)

    with Session(tools.engine) as db:
        record(
            db,
            campaign_id,
            NpcAdded(
                name="Tale",
                location="Yard",
                description="A traveller.",
                personality="",
                accent="",
                quirks=[],
                story_only=True,
            ),
        )
        db.commit()
    with pytest.raises(InvalidInput, match=error):
        tools.update_npc("Tale", character=character)

    with Session(tools.engine) as db:
        state = load_state(db, campaign_id)
        assert state.named("Scout") is None
        assert state.named("Guide") is None
        tale = state.named("Tale")
        assert isinstance(tale, Character) and tale.story_only
