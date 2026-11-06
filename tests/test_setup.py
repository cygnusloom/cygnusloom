from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.config import Settings
from cygnusloom.db.models import LlmModel
from cygnusloom.db.session import make_engine
from cygnusloom.llm.probe import ProbeError, list_models, probe_model
from tests.conftest import SECRET, csrf_token, make_app


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'setup.db'}",
        "secret_key": SECRET,
        "llm_models": (),
        "llm_default_model": "",
        "base_url": "http://127.0.0.1:8000",
        "single_player": True,
    }
    values.update(overrides)
    return Settings(**values)


def _models(settings: Settings) -> list[LlmModel]:
    with Session(make_engine(settings.database_url)) as db:
        return list(db.exec(select(LlmModel)).all())


def test_missing_model_sends_the_player_to_setup(tmp_path):
    client = TestClient(make_app(_settings(tmp_path)))

    response = client.get("/campaigns", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_page_shows_the_presets(tmp_path):
    client = TestClient(make_app(_settings(tmp_path)))

    page = client.get("/setup")

    assert page.status_code == 200
    assert "Give your game master a mind" in page.text
    assert "Ollama" in page.text
    assert "DeepSeek" in page.text
    assert "Scripted demo" in page.text


def test_setup_saves_the_first_model_as_default(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/setup")

    response = client.post(
        "/setup",
        data={
            "csrf_token": token,
            "preset": "ollama",
            "model": "llama3.2:3b",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns/new"
    rows = _models(settings)
    assert len(rows) == 1
    assert rows[0].provider == "ollama"
    assert rows[0].model == "llama3.2:3b"
    assert rows[0].base_url == "http://127.0.0.1:11434/v1"
    assert rows[0].is_default is True
    assert client.get("/campaigns/new").status_code == 200


def test_setup_supports_the_scripted_demo(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/setup")

    response = client.post(
        "/setup",
        data={"csrf_token": token, "preset": "demo"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    rows = _models(settings)
    assert [(row.provider, row.model, row.base_url) for row in rows] == [("fake", "scripted", "")]


def test_setup_rejects_a_missing_model(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/setup")

    response = client.post(
        "/setup",
        data={"csrf_token": token, "preset": "custom", "model": "", "base_url": ""},
    )

    assert response.status_code == 400
    assert "model name is required" in response.text.lower()
    assert _models(settings) == []


def test_setup_requires_csrf(tmp_path):
    client = TestClient(make_app(_settings(tmp_path)))

    response = client.post("/setup", data={"preset": "demo"})

    assert response.status_code == 403


def test_setup_detect_lists_the_demo_model(tmp_path):
    client = TestClient(make_app(_settings(tmp_path)))
    token = csrf_token(client, "/setup")

    response = client.post("/setup/detect", data={"csrf_token": token, "preset": "demo"})

    assert response.status_code == 200
    assert response.json() == {"models": ["scripted"], "error": ""}


def test_setup_test_accepts_the_demo_model(tmp_path):
    client = TestClient(make_app(_settings(tmp_path)))
    token = csrf_token(client, "/setup")

    response = client.post("/setup/test", data={"csrf_token": token, "preset": "demo"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "scripted demo" in response.json()["message"].lower()


def test_upsert_updates_an_existing_model_instead_of_duplicating(tmp_path):
    settings = _settings(tmp_path)
    app = make_app(settings)
    catalog = app.state.catalog

    first = catalog.upsert("ollama", "one", "http://127.0.0.1:11434/v1")
    second = catalog.upsert("ollama", "one", "http://127.0.0.1:9999/v1")

    assert first.model_id == second.model_id
    rows = _models(settings)
    assert len(rows) == 1
    assert rows[0].base_url == "http://127.0.0.1:9999/v1"


def test_probe_model_reports_success_and_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        if request.headers["authorization"] != "Bearer good":
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}}]})

    async def run() -> tuple:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            good = await probe_model(
                "custom", "http://example.test/v1", "good", "demo", client=client
            )
            bad = await probe_model(
                "custom", "http://example.test/v1", "bad", "demo", client=client
            )
        return good, bad

    good, bad = asyncio.run(run())
    assert good.ok is True
    assert "ready" in good.message
    assert bad.ok is False
    assert "HTTP 401" in bad.message


def test_list_models_reads_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "two"}, {"id": "one"}, {"id": "one"}]})

    async def run() -> tuple[str, ...]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await list_models("http://example.test/v1", client=client)

    assert asyncio.run(run()) == ("one", "two")


def test_list_models_explains_an_unreachable_server():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProbeError, match="Could not reach"):
                await list_models("http://example.test/v1", client=client)

    asyncio.run(run())
