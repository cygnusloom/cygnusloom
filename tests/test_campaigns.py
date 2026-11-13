from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, Event
from cygnusloom.llm.base import ChatReply, LLMError, ToolRequest
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import last_activity_text
from cygnusloom.services.events import Turn
from tests.conftest import (
    NARRATE_TEXT,
    csrf_token,
    is_journal_messages,
    is_review_messages,
    make_app,
)
from tests.factories import (
    answer,
    close_opening,
    pending_turn,
    place_pc,
    send_message,
    signed_in_client,
)


def test_the_campaign_major_matches_the_package_version():
    import cygnusloom

    assert int(cygnusloom.__version__.split(".", 1)[0]) == cygnusloom.CAMPAIGN_MAJOR_VERSION


def _signed_in(client_factory: Callable[[], TestClient], email: str = "ada@example.com"):
    return signed_in_client(client_factory, email)


def _new_campaign(
    client: TestClient,
    label: str = "Test chat",
    world_template: str = "testhollow",
    rule_set: str = "rolltest",
):
    token = csrf_token(client, "/campaigns/new")
    data: dict[str, str] = {
        "csrf_token": token,
        "label": label,
        "world_template": world_template,
        "rule_set": rule_set,
    }
    return client.post(
        "/campaigns/new",
        data=data,
        follow_redirects=False,
    )


def _create_character(
    client: TestClient,
    campaign_id: int,
    name: str = "Ada",
    fields: dict[str, str] | None = None,
    pronouns: str | None = "she/her",
    setting: str | None = None,
    table_genres: list[str] | None = None,
):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    form: dict[str, str | list[str]] = {"csrf_token": token, "name": name, **(fields or {})}
    if pronouns is not None:
        form["pronouns"] = pronouns
    if setting is not None:
        form["setting"] = setting
    if table_genres is not None:
        form["table_genres"] = table_genres
    return client.post(
        f"/campaigns/{campaign_id}/creation",
        data=form,
        follow_redirects=False,
    )


def _campaign_id(
    client_factory: Callable[[], TestClient],
    setting: str = "",
    *,
    opening: bool = False,
) -> tuple[TestClient, int]:
    """A campaign and character ready for play.

    The opening model turn is skipped by default: most tests then read or change campaign
    state and never look at the opening itself. Pass ``opening=True`` when a test asserts on
    the opening narration, its journal entry, or the model calls it made.
    """
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    created = _create_character(client, campaign_id, setting=setting)
    assert created.status_code == 303
    if opening:
        _answer(client, campaign_id, "continue")
    else:
        close_opening(client, campaign_id)
    return client, campaign_id


def _place_pc(client: TestClient, campaign_id: int, location: str = "Mossbell Hollow") -> None:
    """Place the player character, as the GM does with move_actor on its first turn."""
    place_pc(client, campaign_id, location)


def _send(client: TestClient, campaign_id: int, text: str):
    return send_message(client, campaign_id, text)


def _set_model(client: TestClient, campaign_id: int, model: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/model",
        data={"csrf_token": token, "model": model},
        follow_redirects=False,
    )


def _delete_campaign(client: TestClient, campaign_id: int, confirm: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/delete",
        data={"csrf_token": token, "confirm": confirm},
        follow_redirects=False,
    )


def _set_label(client: TestClient, campaign_id: int, label: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/label",
        data={"csrf_token": token, "label": label},
        follow_redirects=False,
    )


def _label_of(client: TestClient, campaign_id: int) -> str:
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.name


def _created_campaign_id(response) -> int:
    return int(response.headers["location"].rsplit("/", 1)[1])


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
    return answer(client, campaign_id, kind, content)


def _pending(app: FastAPI, campaign_id: int) -> Turn | None:
    return pending_turn(app, campaign_id)


def test_create_campaign_and_list_it(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    assert _new_campaign(client, label="Sandbox").status_code == 303
    assert "Sandbox" in client.get("/campaigns").text


def test_last_activity_text_shows_a_relative_time_and_date():
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert last_activity_text(now - timedelta(seconds=20), now) == "just now (2026-11-07)"
    assert last_activity_text(now - timedelta(minutes=5), now) == "5 minutes ago (2026-11-07)"
    assert last_activity_text(now - timedelta(hours=1), now) == "1 hour ago (2026-11-07)"
    assert last_activity_text(now - timedelta(days=2), now) == "2 days ago (2026-11-05)"
    # A database time without a timezone is taken as UTC.
    assert last_activity_text(datetime(2026, 11, 7, 11, 0), now) == "1 hour ago (2026-11-07)"
    assert last_activity_text(None, now) == ""


def test_campaign_list_shows_when_the_last_event_was(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    assert _new_campaign(client, label="Active game").status_code == 303
    page = client.get("/campaigns").text
    assert 'class="campaign-activity"' in page
    assert re.search(r"(?:just now|\d+ (?:minute|hour|day)s? ago) \(\d{4}-\d{2}-\d{2}\)", page)


def test_delete_campaign_removes_it_and_its_events(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Doomed")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert _delete_campaign(client, campaign_id, "Doomed").status_code == 303
    page = client.get("/campaigns")
    assert "Doomed" not in page.text
    assert "Campaign deleted." in page.text
    with Session(client.app.state.engine) as db:
        assert db.get(Campaign, campaign_id) is None
        assert db.exec(select(Event).where(Event.campaign_id == campaign_id)).first() is None


def test_delete_campaign_requires_the_exact_label(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Kept")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert _delete_campaign(client, campaign_id, "kept").status_code == 303
    page = client.get("/campaigns")
    assert "Type the campaign label exactly to confirm deletion." in page.text
    assert "Kept" in page.text
    with Session(client.app.state.engine) as db:
        assert db.get(Campaign, campaign_id) is not None


def test_other_accounts_cannot_delete_a_campaign(client_factory: Callable[[], TestClient]):
    owner = _signed_in(client_factory)
    response = _new_campaign(owner, label="Owner's chat")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    intruder = _signed_in(client_factory, "intruder@example.com")
    assert _delete_campaign(intruder, campaign_id, "Owner's chat").status_code == 404
    with Session(owner.app.state.engine) as db:
        assert db.get(Campaign, campaign_id) is not None


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
            last = str(messages[-1].get("content", "")) if messages else ""
            if tools is not None and last.startswith("State management step."):
                finish = next(
                    (spec for spec in tools if spec["function"]["name"] == "finish_state_step"),
                    None,
                )
                if finish is not None:
                    keys = list(finish["function"]["parameters"]["properties"])
                    return ChatReply(
                        text="",
                        tool_requests=(
                            ToolRequest(
                                id=f"checklist-{len(used)}",
                                name="finish_state_step",
                                arguments=json.dumps({key: "no change" for key in keys}),
                            ),
                        ),
                    )
            return ChatReply(text=f"from {model}")

        async def stream(self, model, messages, tools=None):
            yield await self.chat(model, messages, tools)

    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (Echo(), model_id)
    _send(client, campaign_id, "one")
    assert _set_model(client, campaign_id, "ollama/test-model").status_code == 303
    _send(client, campaign_id, "two")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert used == ["fake/scripted"] * 3 + ["ollama/test-model"] * 3
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


def test_blank_label_gets_the_worlds_next_default(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    first = _new_campaign(client, label="")
    second = _new_campaign(client, label="   ")
    assert first.status_code == second.status_code == 303
    assert _label_of(client, _created_campaign_id(first)) == "Test Hollow #1"
    assert _label_of(client, _created_campaign_id(second)) == "Test Hollow #2"


def test_default_labels_number_each_world_separately(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    _new_campaign(client, label="")
    twin_lakes = _new_campaign(client, label="", world_template="twin-lakes", rule_set="cairn2e")
    assert twin_lakes.status_code == 303
    assert _label_of(client, _created_campaign_id(twin_lakes)) == "Twin Lakes #1"


def test_new_campaign_form_asks_for_the_label_last(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert page.index('name="rule_set"') < page.index('name="world_template"')
    assert page.index('name="world_template"') < page.index('name="label"')


def test_setting_belongs_to_the_campaign_options_creation_page(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    first_page = client.get("/campaigns/new").text
    assert 'name="setting"' not in first_page
    assert 'name="table_genres"' not in first_page
    response = _new_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    campaign_id = _created_campaign_id(response)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="setting" rows="6" maxlength="4000"' in page
    assert 'name="table_genres"' in page
    assert page.index("Campaign options") < page.index('name="setting"')
    assert page.index("Campaign options") < page.index("Creative and lore tables")
    assert page.index('name="setting"') < page.index("Your character")
    assert page.index("Creative and lore tables") < page.index("Your character")
    assert "Blank is ignored." in page


def test_create_stores_the_setting_and_story(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Briefed")
    assert response.status_code == 303
    campaign_id = _created_campaign_id(response)
    created = _create_character(client, campaign_id, setting="  A heist in a drowned city.  ")
    assert created.status_code == 303
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.setting == "A heist in a drowned city."


def test_blank_setting_is_ignored(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = _created_campaign_id(response)
    created = _create_character(client, campaign_id, setting="   ")
    assert created.status_code == 303
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.setting == ""


def test_create_rejects_an_overlong_setting_and_keeps_the_text(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = _created_campaign_id(response)
    setting = "x" * 4001
    created = _create_character(client, campaign_id, setting=setting)
    assert created.status_code == 400
    assert "setting and story must be at most 4000 characters" in created.text
    assert setting in created.text
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.setting == ""


def test_creation_stores_the_chosen_table_genres(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Weird west")
    assert response.status_code == 303
    campaign_id = _created_campaign_id(response)
    created = _create_character(client, campaign_id, table_genres=["generic", "historical"])
    assert created.status_code == 303
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert json.loads(campaign.table_genres) == ["generic", "historical"]


def test_creation_keeps_the_rule_sets_table_genres_when_none_are_chosen(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = _created_campaign_id(response)
    assert _create_character(client, campaign_id).status_code == 303
    with Session(client.app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        assert json.loads(campaign.table_genres) == ["generic"]


def test_creation_rejects_an_unknown_table_genre_and_keeps_the_rest(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client)
    assert response.status_code == 303
    campaign_id = _created_campaign_id(response)
    created = _create_character(client, campaign_id, table_genres=["generic", "steampunk"])
    assert created.status_code == 400
    assert "unknown genre" in created.text
    assert 'name="table_genres" value="generic" checked' in created.text
    assert 'name="table_genres" value="steampunk"' not in created.text


def test_campaign_list_keeps_the_label_editor_behind_a_toggle(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    _new_campaign(client, label="Twin Lakes #1")
    page = client.get("/campaigns").text
    details = page.index('<details class="campaign-label">')
    form = page.index('class="campaign-label-form"')
    assert details < form < page.index("</details>", details)
    assert '<summary aria-label="Edit label for Twin Lakes #1">Edit label</summary>' in page


def test_change_campaign_label_from_the_list(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Old label")
    campaign_id = _created_campaign_id(response)
    assert _set_label(client, campaign_id, "New label").status_code == 303
    page = client.get("/campaigns").text
    assert "New label" in page
    assert "Old label" not in page
    assert "Label saved." in page


def test_change_campaign_label_rejects_a_blank_label(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Kept")
    campaign_id = _created_campaign_id(response)
    assert _set_label(client, campaign_id, "   ").status_code == 303
    page = client.get("/campaigns").text
    assert "label must be 1 to 120 characters" in page
    assert _label_of(client, campaign_id) == "Kept"


def test_other_accounts_cannot_change_a_campaign_label(
    client_factory: Callable[[], TestClient],
):
    owner = _signed_in(client_factory)
    response = _new_campaign(owner, label="Owner's chat")
    campaign_id = _created_campaign_id(response)
    intruder = _signed_in(client_factory, "intruder@example.com")
    assert _set_label(intruder, campaign_id, "Mine").status_code == 404
    assert _label_of(owner, campaign_id) == "Owner's chat"


def test_new_campaign_form_offers_world_and_rule_set(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert '<select name="rule_set" required>' in page
    assert '<select name="world_template" required>' in page
    assert '<option value="testhollow"' in page
    assert '<option value="rolltest"' in page


def test_new_campaign_form_shows_a_blurb_for_each_rule_set_and_world(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert 'data-blurb="Test-only rule set with a player roll.' in page
    assert 'data-blurb="A magical forest of mushroom gnomes.' in page


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


def test_create_rejects_a_rule_set_the_world_is_not_played_with(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="twin-lakes", rule_set="worldswithoutnumber")
    assert response.status_code == 400
    assert "only works with the Cairn 2e rule set" in response.text


def test_new_campaign_form_marks_a_world_that_names_its_rule_set(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert '<option value="twin-lakes" data-rule-set="cairn2e"' in page
    assert "/static/new_campaign.js" in page


def test_new_campaign_form_marks_third_party_content_and_offers_credits(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    assert re.search(r'<option value="cairn2e"[^>]*data-third-party="1"', page)
    assert re.search(r'<option value="worldswithoutnumber"[^>]*data-third-party="1"', page)
    assert re.search(r'<option value="twin-lakes"[^>]*data-third-party="1"', page)
    assert re.search(r'<option value="academia-arcana"[^>]*data-third-party="0"', page)
    assert re.search(r'<span data-blurb-credits="rule_set">', page)
    assert re.search(r'<span data-blurb-credits="world_template" hidden>', page)
    assert page.count('<a href="/license">content credits</a>') == 2


def test_new_campaign_form_puts_credits_in_third_party_descriptions(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client,
        label="x" * 121,
        world_template="twin-lakes",
        rule_set="cairn2e",
    )
    assert response.status_code == 400
    page = response.text
    assert re.search(
        r'<span data-blurb-credits="rule_set">\s*See <a href="/license">content credits</a>\.</span>',
        page,
    )
    assert re.search(
        r'<span data-blurb-credits="world_template">\s*See <a href="/license">content credits</a>\.</span>',
        page,
    )


def test_create_stores_the_chosen_settings(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, label="Mushroom court")
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
    last = [c for c in fake.calls if not is_journal_messages(c) and not is_review_messages(c)][-1]
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
    assert last[-1]["content"] == NARRATE_TEXT


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
    assert 'data-view-target="journal"' in page
    assert 'data-view="journal" aria-labelledby="journal-title" hidden' in page
    assert 'data-view-target="characters"' in page
    assert 'data-view="characters"' in page
    assert 'data-view-target="settings"' in page
    assert 'data-view="settings" aria-labelledby="settings-title"' in page
    assert "data-side-toggle" in page
    assert "data-menu-toggle" in page
    assert 'id="site-menu"' in page


def test_debug_panel_is_hidden_and_refused_by_default(client_factory):
    client, campaign_id = _campaign_id(client_factory)
    assert 'id="tab-debug"' not in client.get(f"/campaigns/{campaign_id}").text
    assert _set_invincibility(client, campaign_id, "Ada", True).status_code == 404


def test_debug_panel_turns_invincibility_on_and_off(settings: Settings, fake: FakeProvider):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-debug"' in page
    assert "Make invincible" in page
    assert "Inject tool call" in page
    assert "Inject narration" in page
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
