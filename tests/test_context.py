from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from cygnusforge.config import Settings
from cygnusforge.db.models import Campaign, Event
from cygnusforge.llm.base import ChatReply, LLMError
from cygnusforge.services.campaigns import SUMMARY_PROMPT, system_parts
from cygnusforge.services.events import Channel
from cygnusforge.web.app import create_app
from tests.test_campaigns import _campaign_id, _send

METER_RE = re.compile(r'<span class="muted">([\d,]+) / 32,768</span>')


class RecordingProvider:
    """Records every prompt. Answers summaries with `summary_reply` and chat with fixed text."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.summaries = 0
        self.summary_reply = "SUMMARY"
        self.fail_after_summary = False

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        self.calls.append(messages)
        if messages[0]["content"].startswith(SUMMARY_PROMPT):
            self.summaries += 1
            return ChatReply(text=self.summary_reply)
        if self.fail_after_summary and self.summaries:
            raise LLMError("the model is down")
        return ChatReply(text="reply")

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        yield await self.chat(model, messages, tools)

    def summarizer_calls(self) -> list[list[dict[str, Any]]]:
        return [call for call in self.calls if call[0]["content"].startswith(SUMMARY_PROMPT)]


def _small_window_app(settings: Settings, provider: RecordingProvider) -> FastAPI:
    # A 1000-token window: summaries start at 500 tokens and the verbatim tail is about 100.
    return create_app(
        dataclasses.replace(settings, llm_context_tokens=1000), fake_provider=provider
    )


def _stored(app: FastAPI, campaign_id: int) -> Campaign:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        db.expunge(campaign)
        return campaign


def _saved_message_count(app: FastAPI) -> int:
    with Session(app.state.engine) as db:
        return db.exec(
            select(func.count()).select_from(Event).where(Event.channel == Channel.TRANSCRIPT)
        ).one()


def _send_until_summarized(client: TestClient, app: FastAPI, campaign_id: int, sends: int) -> None:
    for index in range(sends):
        _send(client, campaign_id, f"m{index} " + "x" * 600)
        if _stored(app, campaign_id).context_summary:
            return


def _system_text(rules: dict[str, str], summary: str, through: int = 0) -> str:
    return "\n\n".join(part.content for part in system_parts("freeform", rules, summary, through))


def test_rules_are_added_to_the_system_prompt():
    prompt = _system_text({"consistency": "be consistent", "fourth": "no fourth wall"}, "")
    assert prompt.startswith("You are a helpful assistant.")
    assert "Rules for this campaign:\n- be consistent\n- no fourth wall" in prompt
    assert "Summary of earlier events" not in prompt


def test_summary_is_added_to_the_system_prompt_only_when_present():
    prompt = _system_text({}, "The party met a mushroom sage.")
    assert "Summary of earlier events in this campaign:\nThe party met a mushroom sage." in prompt
    assert "Rules for this campaign" not in prompt


def test_chat_sends_every_rule_set_rule_to_the_model(settings: Settings):
    provider = RecordingProvider()
    app = create_app(settings, fake_provider=provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    assert _send(client, campaign_id, "hello").status_code == 303
    system = provider.calls[0][0]["content"]
    for rule_text in app.state.content.rule_sets["freeform"].rules.values():
        assert rule_text in system


def test_no_summary_when_the_chat_is_well_inside_the_window(settings: Settings):
    provider = RecordingProvider()
    app = create_app(settings, fake_provider=provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    for _ in range(3):
        assert _send(client, campaign_id, "x" * 400).status_code == 303
    assert len(provider.calls) == 6
    assert provider.summarizer_calls() == []
    campaign = _stored(app, campaign_id)
    assert campaign.context_summary == ""
    assert campaign.summary_through == 0


def test_history_is_summarized_at_half_the_window_and_the_tail_is_kept(settings: Settings):
    provider = RecordingProvider()
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    _send_until_summarized(client, app, campaign_id, sends=8)

    campaign = _stored(app, campaign_id)
    assert campaign.context_summary == "SUMMARY"
    assert campaign.summary_through > 0

    [summarizer] = provider.summarizer_calls()
    assert "m0 " in summarizer[1]["content"]
    assert "Keep it under 187 words." in summarizer[0]["content"]

    after = next(
        call for call in provider.calls if "Summary of earlier events" in call[0]["content"]
    )
    assert "SUMMARY" in after[0]["content"]
    older_text = "\n".join(m["content"] for m in after[1:])
    assert "m0 " not in older_text


def test_long_summary_is_capped_to_a_quarter_of_the_window_keeping_the_newest_text(
    settings: Settings,
):
    provider = RecordingProvider()
    provider.summary_reply = "old detail " * 300 + "NEWEST"
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    _send_until_summarized(client, app, campaign_id, sends=8)

    summary = _stored(app, campaign_id).context_summary
    # 25% of a 1000-token window at about four characters per token.
    assert len(summary) <= 1000
    assert summary.endswith("NEWEST")
    assert not summary.startswith(" ")


def test_summary_drops_the_heading_the_model_repeats(settings: Settings):
    provider = RecordingProvider()
    provider.summary_reply = "Summary so far:\n\nThe party met a mushroom sage."
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    _send_until_summarized(client, app, campaign_id, sends=8)
    assert _stored(app, campaign_id).context_summary == "The party met a mushroom sage."


def test_failed_reply_after_summarizing_saves_nothing(settings: Settings):
    provider = RecordingProvider()
    provider.fail_after_summary = True
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    statuses = []
    for index in range(8):
        statuses.append(_send(client, campaign_id, f"m{index} " + "x" * 600).status_code)
        if provider.summarizer_calls():
            break

    assert statuses[-1] == 502, "the summarizing turn should fail"
    assert statuses[:-1] == [303] * (len(statuses) - 1)
    campaign = _stored(app, campaign_id)
    assert campaign.context_summary == ""
    assert campaign.summary_through == 0
    assert _saved_message_count(app) == 2 * (len(statuses) - 1)


def test_transcript_keeps_every_message_after_summarizing(settings: Settings):
    provider = RecordingProvider()
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    sends = 0
    for index in range(8):
        _send(client, campaign_id, f"m{index} " + "x" * 600)
        sends += 1
        if _stored(app, campaign_id).context_summary:
            break
    assert _stored(app, campaign_id).context_summary == "SUMMARY"
    assert _saved_message_count(app) == 2 * sends
    assert "m0 " in client.get(f"/campaigns/{campaign_id}").text


def test_context_meter_sits_before_the_settings_button(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="context-meter"' in page
    assert 'max="32768"' in page
    assert page.index('class="context-meter"') < page.index('class="composer-settings"')


def test_context_meter_shows_the_window_from_settings(settings: Settings):
    app = create_app(
        dataclasses.replace(settings, llm_context_tokens=4096), fake_provider=RecordingProvider()
    )
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    page = client.get(f"/campaigns/{campaign_id}").text
    assert re.search(r'<span class="muted">[\d,]+ / 4,096</span>', page)


def test_context_meter_grows_with_saved_history(settings: Settings):
    app = create_app(settings, fake_provider=RecordingProvider())
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    empty = int(
        METER_RE.search(client.get(f"/campaigns/{campaign_id}").text).group(1).replace(",", "")
    )
    _send(client, campaign_id, "x" * 4000)
    filled = int(
        METER_RE.search(client.get(f"/campaigns/{campaign_id}").text).group(1).replace(",", "")
    )
    assert filled >= empty + 1000
