from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.library import ContentLibrary
from cygnusforge.llm.base import LLMError
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.campaigns import OPENING_PROMPT, opening_prompt
from cygnusforge.services.events import load_state
from tests.conftest import csrf_token
from tests.test_campaigns import (
    _answer,
    _campaign_id,
    _create_character,
    _new_campaign,
    _pending,
    _send,
    _signed_in,
)


def test_opening_starts_the_world_at_its_start_location_with_its_hooks():
    template = ContentLibrary.load().world_templates["twin-lakes"]
    prompt = opening_prompt(template)
    assert prompt.startswith(OPENING_PROMPT)
    assert "The campaign starts at Isthmus Town (location id isthmus-town)" in prompt
    assert "Flavia at the Butcher in Isthmus Town" in prompt
    assert "Reeve Trellis at the Stuffed Inn" in prompt


def test_opening_without_a_world_start_is_the_plain_instruction():
    assert opening_prompt(None) == OPENING_PROMPT


def _created_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A campaign with its player character, before the opening GM turn has run."""
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert _create_character(client, campaign_id).status_code == 303
    return client, campaign_id


def test_character_creation_leaves_the_opening_pending(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _created_campaign(client_factory)
    turn = _pending(app, campaign_id)
    assert turn is not None and turn.opening and turn.prompt is None


def test_opening_runs_without_a_player_message(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    assert _pending(app, campaign_id) is None
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message user"') == 0
    assert page.count('class="message assistant"') == 1


def test_opening_prompt_is_sent_to_the_model(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    fake = FakeProvider()
    app.state.providers = lambda model_id: (fake, model_id)
    client, campaign_id = _created_campaign(client_factory)
    _answer(client, campaign_id, "continue")
    sent = [message for call in fake.calls for message in call]
    assert any(OPENING_PROMPT in str(message.get("content", "")) for message in sent)


def test_players_cannot_act_before_the_opening_finishes(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _created_campaign(client_factory)
    response = _send(client, campaign_id, "I look around.")
    assert response.status_code == 400
    with Session(app.state.engine) as db:
        assert load_state(db, campaign_id).turn.opening
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message user"') == 0


def test_opening_cannot_be_discarded(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _created_campaign(client_factory)
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/pending/discard",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    turn = _pending(app, campaign_id)
    assert turn is not None and turn.opening
    assert "Ada" in client.get(f"/campaigns/{campaign_id}").text


def test_pending_opening_page_auto_starts_and_hides_discard(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _created_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "The GM is opening the campaign." in page
    assert "data-auto-start" in page
    assert 'name="kind" value="continue"' in page
    assert "Discard reply" not in page


def test_failed_opening_offers_continue_instead_of_auto_start(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

        async def stream(self, model, messages, tools=None):
            yield await self.chat(model, messages, tools)

    client, campaign_id = _created_campaign(client_factory)
    app.state.providers = lambda model_id: (Broken(), "model")
    _answer(client, campaign_id, "continue")
    turn = _pending(app, campaign_id)
    assert turn is not None and turn.opening and turn.failed
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "provider broken is unreachable" in page
    assert 'name="kind" value="continue"' in page
    assert "data-auto-start" not in page
    assert "The GM is opening the campaign." not in page


def test_transcript_reports_busy_while_the_opening_runs_and_waiting_when_it_stopped(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

        async def stream(self, model, messages, tools=None):
            yield await self.chat(model, messages, tools)

    client, campaign_id = _created_campaign(client_factory)
    assert 'data-gm-state="busy"' in client.get(f"/campaigns/{campaign_id}").text
    app.state.providers = lambda model_id: (Broken(), "model")
    _answer(client, campaign_id, "continue")
    assert 'data-gm-state="waiting"' in client.get(f"/campaigns/{campaign_id}").text
