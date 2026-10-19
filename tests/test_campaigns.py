from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.llm.base import ChatReply, LLMError
from cygnusforge.llm.fake import FakeProvider
from tests.conftest import csrf_token, register


def _signed_in(client_factory: Callable[[], TestClient], email: str = "ada@example.com"):
    client = client_factory()
    register(client, email)
    return client


def _new_campaign(
    client: TestClient,
    name: str = "Test chat",
    world_template: str = "gnomeforest",
    rule_set: str = "freeform",
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


def _campaign_id(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    return client, int(response.headers["location"].rsplit("/", 1)[1])


def _send(client: TestClient, campaign_id: int, text: str, model: str = "fake/scripted"):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": text, "model": model},
        follow_redirects=False,
    )


def test_create_campaign_and_list_it(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    assert _new_campaign(client, name="Sandbox").status_code == 303
    assert "Sandbox" in client.get("/campaigns").text


def test_send_rejects_unknown_model(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    response = _send(client, campaign_id, "hi", model="openai/gpt-x")
    assert response.status_code == 400
    assert "not available" in response.text
    assert 'class="message ' not in client.get(f"/campaigns/{campaign_id}").text


def test_each_message_can_use_a_different_model(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    used: list[str] = []

    class Echo:
        name = "echo"

        async def chat(self, model, messages, tools=None):
            used.append(model)
            return ChatReply(text=f"from {model}")

    app.state.providers = lambda model_id: (Echo(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "one", model="fake/scripted")
    _send(client, campaign_id, "two", model="ollama/test-model")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert used == ["fake/scripted", "ollama/test-model"]
    assert "from fake/scripted" in page
    assert "from ollama/test-model" in page


def test_send_keeps_the_chosen_model_selected(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Echo:
        name = "echo"

        async def chat(self, model, messages, tools=None):
            return ChatReply(text="ok")

    app.state.providers = lambda model_id: (Echo(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    response = _send(client, campaign_id, "hi", model="ollama/test-model")
    assert response.status_code == 303
    assert response.headers["location"] == (f"/campaigns/{campaign_id}?model=ollama%2Ftest-model")
    page = client.get(response.headers["location"]).text
    assert 'value="ollama/test-model" checked' in page


def test_create_rejects_blank_name(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    assert _new_campaign(client, name="   ").status_code == 400


def test_new_campaign_form_offers_world_and_rule_set(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert '<select name="world_template" required>' in page
    assert '<option value="gnomeforest">Gnome Forest</option>' in page
    assert '<select name="rule_set" required>' in page
    assert '<option value="freeform">Freeform</option>' in page


def test_campaign_shows_the_world_and_rule_set_it_was_created_with(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Gnome Forest · Freeform" in page


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
    assert "Gnome Forest · Freeform" in page


def test_chat_round_trip_saves_both_messages(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    script = '__FAKE__:{"turns": [{"text": "Hi there."}]}'
    assert _send(client, campaign_id, f"hello {script}").status_code == 303
    page = client.get(f"/campaigns/{campaign_id}")
    assert "Hi there." in page.text
    assert "hello" in page.text
    assert page.text.count('class="message ') == 2
    assert '<span class="role">user</span>' in page.text
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
    assert [m["role"] for m in last] == ["system", "user", "assistant", "user"]
    assert last[1]["content"] == "first"
    assert last[-1]["content"] == "second"


def test_scripted_directive_drives_later_turns(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    script = '__FAKE__:{"turns": [{"text": "one"}, {"text": "two"}]}'
    _send(client, campaign_id, f"start {script}")
    _send(client, campaign_id, "next")
    _send(client, campaign_id, "again")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message ') == 6
    assert "You said: again" in page
    assert ">two<" in page and ">one<" in page


def test_scripted_error_turn_saves_nothing(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    script = '__FAKE__:{"turns": [{"error": "upstream exploded"}]}'
    response = _send(client, campaign_id, f"boom {script}")
    assert response.status_code == 502
    assert "upstream exploded" in response.text
    assert 'class="message ' not in client.get(f"/campaigns/{campaign_id}").text


def test_failed_reply_saves_nothing_and_keeps_draft(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

    app.state.providers = lambda model_id: (Broken(), "model")
    client, campaign_id = _campaign_id(client_factory)
    response = _send(client, campaign_id, "are you there?")
    assert response.status_code == 502
    assert "are you there?" in response.text
    assert 'class="message ' not in client.get(f"/campaigns/{campaign_id}").text


def test_empty_message_is_rejected(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    assert _send(client, campaign_id, "  ").status_code == 400
    assert 'class="message ' not in client.get(f"/campaigns/{campaign_id}").text


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
