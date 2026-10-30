from __future__ import annotations

from dataclasses import replace

import pytest
from sqlmodel import Session, select

from cygnusforge.config import Settings
from cygnusforge.db.models import LlmModel
from cygnusforge.db.session import create_schema, make_engine
from cygnusforge.llm.catalog import ModelCatalog, decrypt_api_key
from cygnusforge.services.common import Conflict, InvalidInput


def _catalog(settings: Settings) -> tuple[ModelCatalog, object]:
    engine = make_engine(settings.database_url)
    create_schema(engine)
    return ModelCatalog(engine, settings), engine


def test_environment_models_seed_the_catalog_once(settings: Settings):
    catalog, engine = _catalog(settings)
    assert catalog.seed_from_env() == 2
    assert catalog.seed_from_env() == 0
    assert catalog.source() == "database"
    assert catalog.model_ids() == ("fake/scripted", "ollama/test-model")
    assert catalog.default_model_id() == "fake/scripted"
    with Session(engine) as db:
        rows = db.exec(select(LlmModel)).all()
    assert len(rows) == 2
    assert sum(row.is_default for row in rows) == 1


def test_environment_list_is_used_before_the_first_database_change(settings: Settings):
    catalog, _ = _catalog(settings)
    assert catalog.source() == "environment"
    assert catalog.model_ids() == ("fake/scripted", "ollama/test-model")
    assert catalog.default_model_id() == "fake/scripted"


def test_add_encrypts_the_api_key_and_can_make_a_model_default(settings: Settings):
    catalog, engine = _catalog(settings)
    catalog.seed_from_env()
    spec = catalog.add(
        "openai",
        "gpt-4o-mini",
        "https://api.openai.com/v1",
        "sk-secret",
        is_default=True,
    )
    assert spec.model_id == "openai/gpt-4o-mini"
    assert spec.api_key_set is True
    assert catalog.default_model_id() == "openai/gpt-4o-mini"
    assert catalog.provider_config("openai") == (
        "https://api.openai.com/v1",
        "sk-secret",
    )
    with Session(engine) as db:
        row = db.exec(select(LlmModel).where(LlmModel.provider == "openai")).one()
    assert row.api_key != "sk-secret"
    assert decrypt_api_key(settings.secret_key, row.api_key) == "sk-secret"


def test_updating_blank_api_key_keeps_the_existing_key(settings: Settings):
    catalog, engine = _catalog(settings)
    spec = catalog.add("openai", "gpt-4o-mini", "https://api.openai.com/v1", "sk-secret")
    catalog.update(spec.row_id or 0, base_url="https://proxy.example.com/v1", api_key=None)
    with Session(engine) as db:
        row = db.get(LlmModel, spec.row_id)
    assert row is not None
    assert row.base_url == "https://proxy.example.com/v1"
    assert decrypt_api_key(settings.secret_key, row.api_key) == "sk-secret"

    catalog.update(spec.row_id or 0, base_url="https://api.openai.com/v1", api_key="")
    with Session(engine) as db:
        row = db.get(LlmModel, spec.row_id)
    assert row is not None and row.api_key == ""


def test_disabling_or_deleting_the_default_moves_the_default(settings: Settings):
    catalog, _ = _catalog(settings)
    catalog.seed_from_env()
    fake = next(row for row in catalog.all_specs() if row.model_id == "fake/scripted")
    catalog.set_enabled(fake.row_id or 0, False)
    assert catalog.default_model_id() == "ollama/test-model"
    catalog.set_default(fake.row_id or 0)
    assert fake.model_id == "fake/scripted"
    assert catalog.default_model_id() == "fake/scripted"
    catalog.delete(fake.row_id or 0)
    assert catalog.default_model_id() == "ollama/test-model"


def test_model_validation_rejects_bad_providers_and_missing_base_urls(settings: Settings):
    catalog, _ = _catalog(settings)
    with pytest.raises(InvalidInput, match="provider"):
        catalog.add("bad provider", "model")
    with pytest.raises(InvalidInput, match="base URL"):
        catalog.add("openai", "model")
    with pytest.raises(InvalidInput, match="base URL"):
        catalog.add("ollama", "model")
    catalog.add("openai", "model", "https://api.openai.com/v1")
    with pytest.raises(Conflict, match="already"):
        catalog.add("openai", "model", "https://api.openai.com/v1")


def test_an_empty_environment_has_no_models(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'no-models.db'}", secret_key="test")
    catalog, _ = _catalog(settings)
    assert catalog.model_ids() == ()
    assert catalog.default_model_id() == ""
    assert catalog.source() == "none"
    assert catalog.seed_from_env() == 0


def test_admin_add_on_an_environment_catalog_preserves_existing_models(settings: Settings):
    catalog, _ = _catalog(settings)
    catalog.add("openai", "gpt-4o-mini", "https://api.openai.com/v1")
    assert set(catalog.model_ids()) == {"fake/scripted", "ollama/test-model", "openai/gpt-4o-mini"}


def test_settings_fixture_can_be_replaced_with_no_models(settings: Settings):
    empty = replace(settings, llm_models=(), llm_default_model="")
    catalog, _ = _catalog(empty)
    catalog.seed_from_env()
    assert catalog.model_ids() == ()
