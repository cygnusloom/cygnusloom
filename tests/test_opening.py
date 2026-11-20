from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.llm.base import LLMError
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.llm.registry import ProviderFactory
from cygnusloom.services.events import load_state
from cygnusloom.services.prompts import OPENING_PROMPT, OPENING_REPLY_INSTRUCTION, opening_prompt
from cygnusloom.services.turns import OPENING_REPLY_RETRY
from tests.conftest import bundled_library, csrf_token
from tests.test_campaigns import (
    _answer,
    _campaign_id,
    _create_character,
    _new_campaign,
    _pending,
    _send,
    _signed_in,
)
from tests.test_tools import _recording_provider, _saved, _script


def test_opening_starts_the_world_at_its_start_location_with_its_hooks():
    template = bundled_library().setting_template("twin-lakes")
    prompt = opening_prompt(template)
    assert prompt.startswith(OPENING_PROMPT)
    assert "The campaign starts at Isthmus Town (location id isthmus-town)" in prompt
    assert "Flavia at the Butcher in Isthmus Town" in prompt
    assert "Reeve Trellis at the Stuffed Inn" in prompt


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


def test_an_opening_whose_provider_cannot_start_reports_the_reason(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    """A provider that fails while it is built marks the reply failed, with the reason."""

    class FailingFactory(ProviderFactory):
        def _build(self, *_args):
            raise ssl.SSLError("unknown error")

    client, campaign_id = _created_campaign(client_factory)
    app.state.providers = FailingFactory(app.state.settings, catalog=app.state.catalog)
    events = _answer(client, campaign_id, "continue")
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 502
    assert "could not start" in events[-1]["message"]
    turn = _pending(app, campaign_id)
    assert turn is not None and turn.failed and "could not start" in turn.failed
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "could not start" in page
    assert "data-auto-start" not in page


SHORT_OPENING = "I'm **Cygnus**, your game master."
FULL_OPENING = (
    "I'm **Cygnus**, your game master.\n"
    "The **Twin Lakes** lie a hard day's walk west of the pass.\n"
    "**Ada**, a hollow-born scout, has come back to their shore.\n"
    "Morning lies over the water, and the far bank is quiet.\n"
    "What do you do?"
)


def _use_opening_script(recorder: Any, script: str) -> None:
    """The opening is sent without a player message, so the script rides on its instruction."""
    chat, stream = recorder.chat, recorder.stream

    def with_script(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**m, "content": f"{m['content']}\n{script}"}
            if m["role"] == "user" and OPENING_PROMPT in m["content"]
            else m
            for m in messages
        ]

    async def scripted_chat(model, messages, tools=None):
        return await chat(model, with_script(messages), tools)

    async def scripted_stream(model, messages, tools=None):
        async for item in stream(model, with_script(messages), tools):
            yield item

    recorder.chat = scripted_chat
    recorder.stream = scripted_stream


def test_a_short_opening_reply_is_written_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = _signed_in(client_factory)
    campaign_id = int(_new_campaign(client).headers["location"].rsplit("/", 1)[1])
    assert _create_character(client, campaign_id).status_code == 303
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _use_opening_script(
        recorder,
        _script(
            {
                "rounds": [[{"name": "ooc", "arguments": {"no_narration": True}}]],
                "ooc": SHORT_OPENING,
                "ooc_retry": FULL_OPENING,
            }
        ),
    )

    events = _answer(client, campaign_id, "continue")

    assert _saved(app, campaign_id)[-1] == ("assistant", FULL_OPENING)
    kinds = [event["type"] for event in events]
    assert "retract" in kinds
    retracted_at = kinds.index("retract")
    shown_before = "".join(e["text"] for e in events[:retracted_at] if e["type"] == "delta")
    shown_after = "".join(e["text"] for e in events[retracted_at:] if e["type"] == "delta")
    assert shown_before.strip() == SHORT_OPENING
    assert shown_after.strip() == FULL_OPENING
    prompts = [
        str(message.get("content"))
        for call in recorder.calls
        for message in call["messages"]
        if message["role"] == "system"
    ]
    assert prompts.count(OPENING_REPLY_INSTRUCTION) == 2
    assert any(OPENING_REPLY_RETRY in prompt for prompt in prompts)


def test_a_complete_opening_reply_is_not_written_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = _signed_in(client_factory)
    campaign_id = int(_new_campaign(client).headers["location"].rsplit("/", 1)[1])
    assert _create_character(client, campaign_id).status_code == 303
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _use_opening_script(
        recorder,
        _script(
            {
                "rounds": [[{"name": "ooc", "arguments": {"no_narration": True}}]],
                "ooc": FULL_OPENING,
            }
        ),
    )

    events = _answer(client, campaign_id, "continue")

    assert _saved(app, campaign_id)[-1] == ("assistant", FULL_OPENING)
    assert "retract" not in [event["type"] for event in events]
    prompts = [
        str(message.get("content"))
        for call in recorder.calls
        for message in call["messages"]
        if message["role"] == "system"
    ]
    assert prompts.count(OPENING_REPLY_INSTRUCTION) == 1
    assert all(OPENING_REPLY_RETRY not in prompt for prompt in prompts)
