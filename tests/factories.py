"""Shared setup for tests that need a campaign and a player character.

The helpers go through the app's own routes, so a test gets the same authentication, CSRF,
form validation, and creation services a player does. The neutral ``rolltest`` rule set and the
``testhollow`` world are the defaults: a test that only needs a working campaign can ask for
one in a line.

``scene`` and ``tools_for`` keep a test of generic behavior off a source book: the fixture rule
set carries its own stats, combat, checks, conditions, and counters, so no Cairn statblock or
world content is loaded. Tests about a shipped rule set's own content stay on that content.
"""

from __future__ import annotations

import html
import json
import random
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlmodel import Session, select
from starlette.datastructures import FormData

from cygnusloom.content.character import character_schema
from cygnusloom.content.library import RuleSet
from cygnusloom.db.models import User
from cygnusloom.services import campaigns as campaigns_service
from cygnusloom.services import world
from cygnusloom.services.campaigns import NewCharacter
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CampaignState, Character, ChatGM, Turn, load_state, record
from cygnusloom.services.tools import check_arguments
from cygnusloom.services.world import WorldTools
from cygnusloom.web.character_form import (
    campaign_submission_from_form,
    pronouns_from_form,
    submission_from_form,
)
from tests.conftest import content_library, csrf_token, register, remember_csrf_token

DEFAULT_WORLD = "testhollow"
DEFAULT_RULE_SET = "rolltest"
DEFAULT_LABEL = "Contract Hollow"
DEFAULT_PC = "Ada"
DEFAULT_PRONOUNS = "she/her"
DEFAULT_PLACE = "Mossbell Hollow"

# A stat block with every modifier known, for Worlds Without Number tests that assert exact
# armor, hit points, or Readied capacity. A test that builds a character from the page's own roll
# gets whatever 3d6 said, so one run in twenty has a Strength too low to hold the starting gear;
# that is a real play experience but a useless test, because the number the test compares against
# was written for a different character.
WWN_STATS = {
    "str": 12,
    "dex": 14,
    "con": 10,
    "int": 18,
    "wis": 10,
    "cha": 8,
    # The page rolls these too, so the pin has to cover every rolled stat.
    "hp": 6,
    "attack_bonus": 1,
    "castings": 1,
}


@contextmanager
def pinned_stats(stats: Mapping[str, int]) -> Iterator[None]:
    """Make the creation page roll `stats` instead of dice, for as long as the block runs.

    The page stores its roll in the session, so the pin has to be in place when the page is
    loaded; the creation form then posts those same values.
    """
    with patch.object(
        campaigns_service,
        "roll_stats",
        lambda spec, rng, *, plus: dict(stats),
    ):
        yield


# A form field name may already carry its prefix, as the existing creation helpers send it.
_FIELD_PREFIXES = (
    "field-",
    "stat-",
    "pick-",
    "repeat-feature-",
    "skill-replacement-",
    "campaign-",
    "pronouns-",
    "template-",
)
_TEMPLATE_PREFIX = "template-"


def sign_in(client: TestClient, email: str = "ada@example.com") -> TestClient:
    """Sign a client in as a development account, through the same login route the page uses."""
    register(client, email)
    return client


def signed_in_client(
    client_factory: Callable[[], TestClient], email: str = "ada@example.com"
) -> TestClient:
    """A client with its own cookie jar, signed in as ``email``."""
    return sign_in(client_factory(), email)


def create_campaign(
    client: TestClient,
    *,
    label: str = DEFAULT_LABEL,
    world_template: str = DEFAULT_WORLD,
    rule_set: str = DEFAULT_RULE_SET,
) -> int:
    """Create one campaign for the signed-in client and return its id."""
    token = csrf_token(client, "/campaigns/new")
    response = client.post(
        "/campaigns/new",
        data={
            "csrf_token": token,
            "label": label,
            "world_template": world_template,
            "rule_set": rule_set,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


STAT_SELECT_RE = re.compile(r'<select name="stat-(\w+)"[^>]*>(.*?)</select>', re.DOTALL)
STAT_HIDDEN_RE = re.compile(r'<input type="hidden" name="stat-(\w+)" value="(\d+)">')
CHOICE_RE = re.compile(r'<input type="radio" name="((?:template|field)-[\w-]+)" value="([^"]*)"')


def creation_fields(client: TestClient, campaign_id: int) -> dict[str, str]:
    """The rolled stats and the first choice of every radio group, as the creation form sends
    them. A rule set that rolls stats needs this before ``create_character``."""
    page = client.get(f"/campaigns/{campaign_id}").text
    rolled = {
        stat: int(re.search(r'value="(\d+)" selected', body).group(1))
        for stat, body in STAT_SELECT_RE.findall(page)
    }
    rolled.update({stat: int(value) for stat, value in STAT_HIDDEN_RE.findall(page)})
    fields = {f"stat-{stat}": str(value) for stat, value in rolled.items()}
    for name, value in CHOICE_RE.findall(page):
        fields.setdefault(name, html.unescape(value))
    return fields


def choose_campaign_options(client: TestClient, campaign_id: int, rule_set: str) -> None:
    """Choose the first value of every campaign option, as the creation form does."""
    rules = content_library().rule_sets[rule_set]
    if not rules.campaign.fields:
        return
    options: dict[str, str] = {}
    for field in rules.campaign.fields.values():
        if field.table is not None:
            options[field.id] = next(iter(rules.campaign.tables[field.table])).value
        else:
            options[field.id] = "A test."
    with db(client.app) as session:
        world.set_campaign_options(session, campaign_id, rules.campaign, options)
        session.commit()


def create_character(
    client: TestClient,
    campaign_id: int,
    *,
    name: str = DEFAULT_PC,
    fields: Mapping[str, str] | None = None,
    templates: Mapping[str, str] | None = None,
    pronouns: str | None = DEFAULT_PRONOUNS,
) -> Response:
    """Submit the campaign's creation form and return the response.

    ``fields`` are character fields by id, such as ``{"appearance": "Tall."}``, or form names
    that already carry their prefix, such as ``{"stat-hp": "6"}``. ``templates`` map a template
    kind to its id, such as ``{"role": "test-role"}``; a ``template-`` prefixed key is also
    accepted unchanged. A test that needs the rolled stat values can read the creation page
    first and pass them as ``stat-`` fields.
    """
    # The page rolls the stats into the session when the character is still pending, so load it
    # before submitting. It also carries the CSRF token, which the same page refresh can change.
    page = client.get(f"/campaigns/{campaign_id}").text
    token = remember_csrf_token(client, page)
    # A browser posts checked boxes, and both tracking switches start checked.
    form = {
        "csrf_token": token,
        "name": name,
        "use_inventory_space": "1",
        "use_survival_needs": "1",
    }
    if pronouns is not None:
        form["pronouns"] = pronouns
    for field_name, value in (fields or {}).items():
        form[_field_form_name(field_name)] = str(value)
    for kind, template_id in (templates or {}).items():
        form[_template_form_name(kind)] = str(template_id)
    return client.post(
        f"/campaigns/{campaign_id}/creation",
        data=form,
        follow_redirects=False,
    )


def _account_id(client: TestClient, email: str) -> int:
    """The row id of the account the client signed in with."""
    with Session(client.app.state.engine) as session:
        account = session.exec(select(User).where(User.email == email)).one()
    return account.id


def create_campaign_direct(
    client: TestClient,
    *,
    label: str = DEFAULT_LABEL,
    world_template: str = DEFAULT_WORLD,
    rule_set: str = DEFAULT_RULE_SET,
    model: str | None = None,
    email: str = "ada@example.com",
) -> int:
    """Create a campaign through the service the new-campaign form calls.

    Use it to set up a test that only needs a campaign to exist. Tests of the form and route
    itself use the HTTP helpers.
    """
    app = client.app
    catalog = app.state.catalog
    campaign = campaigns_service.create_campaign(
        app.state.engine,
        app.state.content,
        catalog.model_ids(),
        _account_id(client, email),
        label,
        world_template,
        rule_set,
        model or catalog.default_model_id(),
    )
    return campaign.id


def create_character_direct(
    client: TestClient,
    campaign_id: int,
    *,
    name: str = DEFAULT_PC,
    fields: Mapping[str, str] | None = None,
    rolled: Mapping[str, int] | None = None,
    pronouns: str = DEFAULT_PRONOUNS,
    setting: str | None = None,
    table_genres: Sequence[str] | None = None,
    email: str = "ada@example.com",
) -> None:
    """Create the player character through the service the creation form submits.

    ``fields`` use the form's own names (``stat-``, ``template-``, ``pick-``, ...). The
    rolled stats default to the submitted ``stat-`` values. Use it to set up a test that is
    not about the creation form; tests of the form and route itself post the page.
    """
    app = client.app
    library = app.state.content
    form = FormData([("name", name), ("pronouns", pronouns), *(fields or {}).items()])
    with Session(app.state.engine) as session:
        campaign = session.get(campaigns_service.Campaign, campaign_id)
        assert campaign is not None
        account_id = session.exec(select(User).where(User.email == email)).one().id
        state = load_state(session, campaign_id)
    spec = campaigns_service.character_spec(library, campaign)
    submitted = submission_from_form(spec, form)
    problem = check_arguments(character_schema(spec), submitted)
    if problem:
        raise InvalidInput(problem)
    options = None
    campaign_spec = campaigns_service.campaign_spec(library, campaign)
    if campaign_spec.fields and not state.options:
        options = campaign_submission_from_form(campaign_spec, form)
    if rolled is None:
        rolled = {
            field.removeprefix("stat-"): int(value)
            for field, value in (fields or {}).items()
            if field.startswith("stat-")
        }
    campaigns_service.save_creation(
        app.state.engine,
        library,
        account_id,
        campaign_id,
        options=options,
        character=NewCharacter(
            name=name,
            rolled=rolled,
            submitted=submitted,
            pronouns=pronouns_from_form(form),
        ),
        setting=setting,
        table_genres=table_genres,
    )


def open_campaign(app: FastAPI, campaign_id: int) -> CampaignState:
    """The campaign state as its recorded events read it now."""
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def db(app: FastAPI) -> Session:
    """A session on the app's database that keeps loaded rows readable after the block."""
    return Session(app.state.engine, expire_on_commit=False)


def character_named(app: FastAPI, campaign_id: int, name: str) -> Character:
    """The character the campaign's events read back, found by name regardless of case."""
    return open_campaign(app, campaign_id).characters[name.casefold()]


def rules_for(rule_set: str = DEFAULT_RULE_SET) -> RuleSet:
    """A loaded rule set from the test content tree."""
    return content_library().rule_sets[rule_set]


def tools_for(
    app: FastAPI,
    campaign_id: int,
    *,
    rule_set: str = DEFAULT_RULE_SET,
    rng: random.Random | None = None,
) -> WorldTools:
    """World tools built from a rule set alone, for a campaign on that rule set.

    This is the app's tool wiring without a world template's own content, which is all a test of
    generic behavior needs: the fixture rule sets carry their own tables, stats, combat, checks,
    conditions, and counters. A test that needs a world's statblocks or tables should ask the app
    for the tools it builds, or pass that content itself.
    """
    rules = rules_for(rule_set)
    return WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=dict(rules.tables),
        rng=rng if rng is not None else random.Random(0),
        character=rules.character,
        rule_data=rules.rule_data,
        grid=rules.grid,
        terrain=rules.terrain,
        encounter_objects=rules.encounter_objects,
        encounter_templates=rules.encounter_templates,
        combat=rules.combat,
        monsters=dict(rules.monsters),
        checks=rules.checks,
        conditions=rules.conditions,
        projects=rules.projects,
        faction_assets=rules.faction_assets,
        counters=rules.counters,
        advancements=rules.advancements,
        level_ups=rules.level_ups,
        periods=rules.periods,
        tallies=rules.tallies,
        relations=rules.relations,
        moves=rules.moves,
        dice_tables=rules.dice_tables,
        clock_rules=rules.clock,
        rests=rules.rests,
        interrupted_rest=rules.interrupted_rest,
        hooks=rules.hooks,
        condition_ends=rules.condition_ends,
        inventory=rules.inventory,
        item_sizes=rules.item_sizes,
        magic=rules.magic,
        marketplace=rules.marketplace,
        plus=rules.plus,
        npc_hp=rules.npc_hp,
        npc_hp_stat=rules.npc_hp_stat,
        travel_modes=rules.travel,
        journey=rules.journey,
        wilderness=rules.wilderness,
        world_terrain=rules.world_terrain,
        housekeeping=rules.housekeeping,
    )


def scene(
    client_factory: Callable[[], TestClient],
    *,
    rule_set: str = DEFAULT_RULE_SET,
    world_template: str = DEFAULT_WORLD,
    name: str = DEFAULT_PC,
    rng: random.Random | None = None,
) -> tuple[FastAPI, int, WorldTools]:
    """A campaign with one character, and tools to act with it: the app, its id, and the tools."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template=world_template, rule_set=rule_set)
    create_character_direct(client, campaign_id, name=name)
    return client.app, campaign_id, tools_for(client.app, campaign_id, rule_set=rule_set, rng=rng)


def campaign_ready(
    client_factory: Callable[[], TestClient],
    *,
    label: str = DEFAULT_LABEL,
    world_template: str = DEFAULT_WORLD,
    rule_set: str = DEFAULT_RULE_SET,
    name: str = DEFAULT_PC,
) -> tuple[TestClient, int]:
    """A campaign whose opening GM turn has finished, so the player can act."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, label=label, world_template=world_template, rule_set=rule_set
    )
    create_character_direct(client, campaign_id, name=name)
    answer(client, campaign_id, "continue")
    return client, campaign_id


def close_opening(client: TestClient, campaign_id: int) -> None:
    """Close a campaign's opening with a recorded test reply instead of running the model turn.
    Use it only when a test reads campaign state and never asserts on the opening narration."""
    with db(client.app) as session:
        record(session, campaign_id, ChatGM(content="The opening plays out.", model="test"))
        session.commit()


def campaign_ready_fast(
    client_factory: Callable[[], TestClient],
    *,
    label: str = DEFAULT_LABEL,
    world_template: str = DEFAULT_WORLD,
    rule_set: str = DEFAULT_RULE_SET,
    name: str = DEFAULT_PC,
) -> tuple[TestClient, int]:
    """A campaign and character whose opening is closed with a recorded test reply instead of
    running the model turn. Use it only when a test reads campaign state and never asserts on
    the opening narration."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, label=label, world_template=world_template, rule_set=rule_set
    )
    create_character_direct(client, campaign_id, name=name)
    close_opening(client, campaign_id)
    return client, campaign_id


def place_pc(
    client: TestClient,
    campaign_id: int,
    location: str = DEFAULT_PLACE,
    name: str = DEFAULT_PC,
) -> None:
    """Place the player character, as the GM does with move_actor on its first turn."""
    with db(client.app) as session:
        world.move_actor(session, campaign_id, name, location)


def send_message(client: TestClient, campaign_id: int, text: str) -> Response:
    """Send a player message to the campaign, without following the redirect."""
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": text},
        follow_redirects=False,
    )


def answer(client: TestClient, campaign_id: int, kind: str, content: str = "") -> list[dict]:
    """Answer the campaign's pending question and return the streamed reply events."""
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/pending/stream",
        data={"csrf_token": token, "kind": kind, "content": content},
    )
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def pending_turn(app: FastAPI, campaign_id: int) -> Turn | None:
    """The turn waiting on the player, or None when the GM has finished."""
    with db(app) as session:
        return load_state(session, campaign_id).turn


class Rolls(random.Random):
    """Dice that show the given values, in order. A value outside the die's range fails the test."""

    def __init__(self, *values: int):
        super().__init__(0)
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high, f"a die of {low} to {high} cannot show {value}"
        return value


def _field_form_name(name: str) -> str:
    if name.startswith(_FIELD_PREFIXES):
        return name
    return f"field-{name}"


def _template_form_name(kind: str) -> str:
    if kind.startswith(_TEMPLATE_PREFIX):
        return kind
    return f"{_TEMPLATE_PREFIX}{kind}"
