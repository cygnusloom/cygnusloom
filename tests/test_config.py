from __future__ import annotations

import pytest

from cygnusforge.config import load_settings

BASE = {"CYGNUSFORGE_SECRET_KEY": "test-secret-key-0123456789abcdef"}


def test_default_model_is_offered_even_when_not_listed():
    settings = load_settings(
        {
            **BASE,
            "CYGNUSFORGE_LLM_PROVIDERS": "deepseek/deepseek-flash",
            "CYGNUSFORGE_LLM_DEFAULT_MODEL": "ollama/gemma4:e2b",
        }
    )
    assert settings.llm_default_model == "ollama/gemma4:e2b"
    assert settings.llm_models == ("ollama/gemma4:e2b", "deepseek/deepseek-flash")


def test_default_model_alone_is_enough():
    settings = load_settings({**BASE, "CYGNUSFORGE_LLM_DEFAULT_MODEL": "deepseek/deepseek-flash"})
    assert settings.llm_models[0] == "deepseek/deepseek-flash"
    assert settings.llm_default_model == "deepseek/deepseek-flash"


def test_secret_key_is_required():
    with pytest.raises(RuntimeError, match="CYGNUSFORGE_SECRET_KEY"):
        load_settings({})
