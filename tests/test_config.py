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


def test_context_window_defaults_to_32k_tokens():
    assert load_settings(BASE).llm_context_tokens == 32768


def test_context_window_can_be_set():
    settings = load_settings({**BASE, "CYGNUSFORGE_LLM_CONTEXT_TOKENS": " 8192 "})
    assert settings.llm_context_tokens == 8192


@pytest.mark.parametrize("value", ["0", "-5", "lots", "1.5", ""])
def test_context_window_must_be_a_positive_whole_number(value: str):
    with pytest.raises(RuntimeError, match="CYGNUSFORGE_LLM_CONTEXT_TOKENS"):
        load_settings({**BASE, "CYGNUSFORGE_LLM_CONTEXT_TOKENS": value})
