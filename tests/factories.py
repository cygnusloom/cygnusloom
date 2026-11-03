"""Shared setup for tests that need a campaign and a player character.

The helpers go through the app's own routes, so a test gets the same authentication, CSRF,
form validation, and creation services a player does. The neutral ``rolltest`` rule set and the
``testhollow`` world are the defaults: a test that only needs a working campaign can ask for
one in a line.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlmodel import Session

from cygnusforge.services.events import CampaignState, load_state
from tests.conftest import csrf_token, register

DEFAULT_WORLD = "testhollow"
DEFAULT_RULE_SET = "rolltest"
DEFAULT_LABEL = "Contract Hollow"
DEFAULT_PC = "Ada"
DEFAULT_PRONOUNS = "she/her"

# A form field name may already carry its prefix, as the existing creation helpers send it.
_FIELD_PREFIXES = (
    "field-",
    "stat-",
    "pick-",
    "repeat-feature-",
    "skill-replacement-",
    "campaign-",
    "pronouns-",
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
    # before submitting. It also keeps the CSRF token and the roll in sync.
    client.get(f"/campaigns/{campaign_id}")
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    form = {"csrf_token": token, "name": name}
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


def open_campaign(app: FastAPI, campaign_id: int) -> CampaignState:
    """The campaign state as its recorded events read it now."""
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def _field_form_name(name: str) -> str:
    if name.startswith(_FIELD_PREFIXES):
        return name
    return f"field-{name}"


def _template_form_name(kind: str) -> str:
    if kind.startswith(_TEMPLATE_PREFIX):
        return kind
    return f"{_TEMPLATE_PREFIX}{kind}"
