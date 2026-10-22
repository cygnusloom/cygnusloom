from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.config import Settings
from cygnusforge.llm.base import ChatReply, LLMError
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services import world
from cygnusforge.services.campaigns import NARRATE_INSTRUCTION
from cygnusforge.services.events import Turn, load_state
from tests.conftest import csrf_token, make_app, register


def _signed_in(client_factory: Callable[[], TestClient], email: str = "ada@example.com"):
    client = client_factory()
    register(client, email)
    return client


def _new_campaign(
    client: TestClient,
    name: str = "Test chat",
    world_template: str = "testhollow",
    rule_set: str = "rolltest",
):
    token = csrf_token(client, "/campaigns/new")
    return client.post(
        "/campaigns/new",
        data={
            "csrf_token": token,
            "name": name,
            "world_template": world_template,
            "rule_set": rule_set,
        },
        follow_redirects=False,
    )


def _create_character(
    client: TestClient,
    campaign_id: int,
    name: str = "Ada",
    fields: dict[str, str] | None = None,
):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/character",
        data={"csrf_token": token, "name": name, **(fields or {})},
        follow_redirects=False,
    )


def _campaign_id(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A campaign whose opening GM turn has finished, so players can act."""
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    created = _create_character(client, campaign_id)
    assert created.status_code == 303
    _answer(client, campaign_id, "continue")
    return client, campaign_id


def _place_pc(client: TestClient, campaign_id: int, location: str = "Mossbell Hollow") -> None:
    """Place the player character, as the GM does with move_actor on its first turn."""
    with Session(client.app.state.engine, expire_on_commit=False) as db:
        world.move_actor(db, campaign_id, "Ada", location)


def _send(client: TestClient, campaign_id: int, text: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": text},
        follow_redirects=False,
    )


def _set_model(client: TestClient, campaign_id: int, model: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/model",
        data={"csrf_token": token, "model": model},
        follow_redirects=False,
    )


def _set_invincibility(client: TestClient, campaign_id: int, name: str, active: bool):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/debug/invincibility",
        data={"csrf_token": token, "name": name, "active": "1" if active else "0"},
        follow_redirects=False,
    )


def _set_debug_stat(client: TestClient, campaign_id: int, name: str, stat: str, remaining: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/debug/stat",
        data={"csrf_token": token, "name": name, "stat": stat, "remaining": remaining},
        follow_redirects=False,
    )


def _debug_client_factory(settings: Settings, fake: FakeProvider) -> Callable[[], TestClient]:
    app = make_app(replace(settings, debug_panel=True), fake_provider=fake)
    return lambda: TestClient(app)


def _answer(client: TestClient, campaign_id: int, kind: str, content: str = "") -> list[dict]:
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/pending/stream",
        data={"csrf_token": token, "kind": kind, "content": content},
    )
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _pending(app: FastAPI, campaign_id: int) -> Turn | None:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id).turn


def test_create_campaign_and_list_it(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    assert _new_campaign(client, name="Sandbox").status_code == 303
    assert "Sandbox" in client.get("/campaigns").text


def test_model_change_rejects_unknown_model(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    response = _set_model(client, campaign_id, "openai/gpt-x")
    assert response.status_code == 400
    assert "not available" in response.text
    assert 'value="fake/scripted" checked' in client.get(f"/campaigns/{campaign_id}").text


def test_model_change_applies_to_the_next_message(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    used: list[str] = []

    class Echo:
        name = "echo"

        async def chat(self, model, messages, tools=None):
            used.append(model)
            return ChatReply(text=f"from {model}")

        async def stream(self, model, messages, tools=None):
            yield await self.chat(model, messages, tools)

    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (Echo(), model_id)
    _send(client, campaign_id, "one")
    assert _set_model(client, campaign_id, "ollama/test-model").status_code == 303
    _send(client, campaign_id, "two")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert used == ["fake/scripted"] * 2 + ["ollama/test-model"] * 2
    assert "from fake/scripted" in page
    assert "from ollama/test-model" in page


def test_model_change_is_saved_on_the_campaign(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    response = _set_model(client, campaign_id, "ollama/test-model")
    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}"
    page = client.get(response.headers["location"]).text
    assert 'value="ollama/test-model" checked' in page
    assert "· ollama/test-model" in page


def test_create_rejects_blank_name(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    assert _new_campaign(client, name="   ").status_code == 400


def test_new_campaign_form_offers_world_and_rule_set(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert '<select name="world_template" required>' in page
    assert '<option value="testhollow">Test Hollow</option>' in page
    assert '<select name="rule_set" required>' in page
    assert '<option value="rolltest">Roll Test</option>' in page


def test_campaign_shows_the_world_and_rule_set_it_was_created_with(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Test Hollow · Roll Test" in page


def test_create_rejects_unknown_world_template(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="mars")
    assert response.status_code == 400
    assert "world template is not available" in response.text


def test_create_rejects_unknown_rule_set(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, rule_set="d20-everything")
    assert response.status_code == 400
    assert "rule set is not available" in response.text


def test_create_stores_the_chosen_settings(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, name="Mushroom court")
    assert response.status_code == 303
    campaign_id = response.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Mushroom court" in page
    assert "Test Hollow · Roll Test" in page


def test_chat_round_trip_saves_both_messages(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    script = '__FAKE__:{"turns": [{"text": "Hi there."}]}'
    assert _send(client, campaign_id, f"hello {script}").status_code == 303
    page = client.get(f"/campaigns/{campaign_id}")
    assert "Hi there." in page.text
    assert "hello" in page.text
    assert page.text.count('class="message ') == 3
    assert '<span class="role">Ada (you)</span>' in page.text
    assert 'data-player-name="Ada"' in page.text
    assert '<span class="role">Cygnus' in page.text
    assert '<span class="role">assistant' not in page.text


def test_default_fake_reply_echoes_last_message(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "ping")
    assert "You said: ping" in client.get(f"/campaigns/{campaign_id}").text


def test_messages_render_markdown_and_escape_html(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "**bold** `code` <script>alert(1)</script> [x](javascript:alert(1))")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "<strong>bold</strong>" in page
    assert "<code>code</code>" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    assert 'href="javascript' not in page


def test_history_is_sent_to_the_model(client_factory: Callable[[], TestClient], fake: FakeProvider):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "first")
    _send(client, campaign_id, "second")
    last = fake.calls[-1]
    assert [m["role"] for m in last] == [
        "system",
        "assistant",
        "user",
        "assistant",
        "user",
        "system",
    ]
    assert last[2]["content"] == "first"
    assert last[4]["content"] == "second"
    assert last[-1]["content"] == NARRATE_INSTRUCTION


def test_scripted_directive_drives_later_turns(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    script = '__FAKE__:{"turns": [{"text": "one"}, {"text": "two"}]}'
    _send(client, campaign_id, f"start {script}")
    _send(client, campaign_id, "next")
    _send(client, campaign_id, "again")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message ') == 7
    assert "You said: again" in page
    assert ">two<" in page and ">one<" in page


def test_scripted_error_turn_keeps_the_message_and_the_open_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    script = '__FAKE__:{"turns": [{"error": "upstream exploded"}]}'
    response = _send(client, campaign_id, f"boom {script}")
    assert response.status_code == 502
    assert "upstream exploded" in response.text
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message user"') == 1
    assert "upstream exploded" in page
    assert _pending(app, campaign_id) is not None


def test_failed_reply_keeps_the_message_and_offers_continue(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

        async def stream(self, model, messages, tools=None):
            yield await self.chat(model, messages, tools)

    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (Broken(), "model")
    response = _send(client, campaign_id, "are you there?")
    assert response.status_code == 502
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message user"') == 1
    assert "are you there?" in page
    assert "provider broken is unreachable" in page
    assert 'name="kind" value="continue"' in page
    turn = _pending(app, campaign_id)
    assert turn is not None and turn.prompt is None and turn.failed


def test_a_failure_leaves_the_transcript_once_the_continued_reply_goes_through(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

        async def stream(self, model, messages, tools=None):
            yield await self.chat(model, messages, tools)

    client, campaign_id = _campaign_id(client_factory)
    working = app.state.providers
    app.state.providers = lambda model_id: (Broken(), "model")
    _send(client, campaign_id, "are you there?")
    assert "provider broken is unreachable" in client.get(f"/campaigns/{campaign_id}").text
    app.state.providers = working
    _answer(client, campaign_id, "continue")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "provider broken is unreachable" not in page
    assert "The GM stopped" not in page
    assert _pending(app, campaign_id) is None or not _pending(app, campaign_id).failed


def test_empty_message_is_rejected(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    assert _send(client, campaign_id, "  ").status_code == 400
    assert client.get(f"/campaigns/{campaign_id}").text.count('class="message ') == 1


def test_other_accounts_cannot_see_or_post(client_factory: Callable[[], TestClient]):
    owner, campaign_id = _campaign_id(client_factory)
    intruder = _signed_in(client_factory, "intruder@example.com")
    assert intruder.get(f"/campaigns/{campaign_id}").status_code == 404
    assert _send(intruder, campaign_id, "hi").status_code == 404
    assert "Test chat" not in intruder.get("/campaigns").text
    assert "Test chat" in owner.get("/campaigns").text


def test_campaign_page_has_sidebar_controls(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-view-target="lore"' in page
    assert 'data-view="lore" aria-label="Lore" hidden' in page
    assert 'data-dialog-target="dialog-characters"' in page
    assert '<dialog id="dialog-characters"' in page
    assert 'data-drawer-target="drawer-notes"' in page
    assert '<aside class="drawer" id="drawer-notes"' in page


def test_debug_panel_is_hidden_and_refused_by_default(client_factory):
    client, campaign_id = _campaign_id(client_factory)
    assert 'id="tab-debug"' not in client.get(f"/campaigns/{campaign_id}").text
    assert _set_invincibility(client, campaign_id, "Ada", True).status_code == 404


def test_debug_panel_turns_invincibility_on_and_off(settings: Settings, fake: FakeProvider):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-debug"' in page
    assert "Make invincible" in page
    assert _set_invincibility(client, campaign_id, "Ada", True).status_code == 303
    assert "Stop invincibility" in client.get(f"/campaigns/{campaign_id}").text
    assert _set_invincibility(client, campaign_id, "Ada", False).status_code == 303
    assert "Make invincible" in client.get(f"/campaigns/{campaign_id}").text


def test_debug_panel_refuses_a_name_that_is_not_a_player_character(
    settings: Settings, fake: FakeProvider
):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))
    assert _set_invincibility(client, campaign_id, "Nobody", True).status_code == 400


def test_debug_panel_refuses_a_bad_score(settings: Settings, fake: FakeProvider):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))
    assert "Set score" in client.get(f"/campaigns/{campaign_id}").text
    assert _set_debug_stat(client, campaign_id, "Ada", "hp", "lots").status_code == 400
    assert _set_debug_stat(client, campaign_id, "Nobody", "hp", "2").status_code == 400


def test_debug_stat_route_is_hidden_by_default(client_factory):
    client, campaign_id = _campaign_id(client_factory)
    assert _set_debug_stat(client, campaign_id, "Ada", "hp", "2").status_code == 404
