from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from cygnusforge.content.character import check_choices
from cygnusforge.web.character_form import creation_view, submission_from_form
from tests.conftest import content_library
from tests.test_campaigns import _new_campaign, _signed_in

SPEC = content_library().rule_sets["hogwarts-rpg"].character
STATS = [("stat-" + stat.id, "0") for stat in SPEC.stats.values()]
YEAR_3 = "3rd Year"


def _spell_of(year: str) -> str:
    return next(
        feature.id
        for feature in SPEC.features.values()
        if feature.feature_type == "spell" and feature.tag == year
    )


def _base_form(blood: str, extra: list[tuple[str, str]]) -> FormData:
    """A complete Hogwarts character form: every table field takes its first option, except the
    Year, which is set to a third-year, with the chosen blood status and extras."""
    pairs = [*STATS, ("template-house", "gryffindor"), ("template-blood", blood)]
    for field in SPEC.fields.values():
        if field.table is not None:
            value = SPEC.tables[field.table][0].value
            pairs.append(("field-" + field.id, YEAR_3 if field.id == "year" else value))
    return FormData(pairs + extra)


def test_spell_picks_from_the_form_are_submitted_as_features():
    spell = _spell_of(YEAR_3)
    form = _base_form("pure-blood", [("pick-pure-blood-0", spell)])
    submitted = submission_from_form(SPEC, form)
    assert submitted["features"] == [spell]
    check_choices(SPEC, submitted)


def test_blank_spell_picks_are_left_out_so_they_are_rolled():
    form = _base_form("pure-blood", [("pick-pure-blood-0", ""), ("pick-pure-blood-1", "")])
    assert "features" not in submission_from_form(SPEC, form)


def test_picks_of_a_template_that_is_not_chosen_are_ignored():
    spell = _spell_of(YEAR_3)
    form = _base_form("muggle-born", [("pick-pure-blood-0", spell)])
    assert "features" not in submission_from_form(SPEC, form)


def test_creation_view_lists_the_spells_a_blood_status_can_pick_by_year():
    view = creation_view(SPEC, {stat: 0 for stat in SPEC.stats})
    blood = next(group for group in view["kinds"] if group["kind"] == "blood")
    pure = next(t for t in blood["templates"] if t["id"] == "pure-blood")
    assert pure["picks"]["count"] == 3
    tags = [group["tag"] for group in pure["picks"]["groups"]]
    assert "3rd Year" in tags
    third = next(group for group in pure["picks"]["groups"] if group["tag"] == YEAR_3)
    assert _spell_of(YEAR_3) in {feature["id"] for feature in third["features"]}
    house = next(group for group in view["kinds"] if group["kind"] == "house")
    assert all(t["picks"] is None for t in house["templates"])


def test_the_hogwarts_creation_page_offers_spell_picks_for_blood_statuses(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="hogwarts", rule_set="hogwarts-rpg")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-spell-picks="pure-blood"' in page
    assert 'name="pick-pure-blood-2"' in page
    assert 'name="pick-muggle-born-0"' in page
    assert 'name="pick-muggle-born-1"' not in page
    assert '<optgroup label="3rd Year" data-tag="3rd Year">' in page
    assert "data-randomize-spells" in page
    assert 'data-tag-field="field-year"' in page


def test_pool_stats_are_marked_assigned_within_their_swap_group():
    view = creation_view(SPEC, {stat: 0 for stat in SPEC.stats})
    assert view["stats"]
    for stat in view["stats"]:
        assert stat["group"] == "traits"
        assert stat["assigned"] is True


def test_the_favourite_subject_view_restricts_elective_options_by_year():
    view = creation_view(SPEC, {stat: 0 for stat in SPEC.stats})
    field = next(f for f in view["choice_fields"] if f["id"] == "favourite-subject")
    assert field["restriction"] == {
        "field": "year",
        "values": ["1st Year", "2nd Year"],
        "groups": ["core"],
    }
    assert {option["group"] for option in field["options"]} == {"core", "elective"}
    assert next(f for f in view["choice_fields"] if f["id"] == "year")["restriction"] is None


def test_the_favourite_subject_page_marks_its_restriction_and_option_groups(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="hogwarts", rule_set="hogwarts-rpg")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-restrict-field="field-year"' in page
    assert 'data-restrict-values=\'["1st Year", "2nd Year"]\'' in page
    assert "data-restrict-groups='[\"core\"]'" in page
    assert 'name="field-favourite-subject" value="Potions"' in page
    assert 'data-group="core"' in page
    assert 'data-group="elective"' in page
