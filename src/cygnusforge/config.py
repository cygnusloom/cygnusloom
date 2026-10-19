from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_MODELS = ("ollama/gemma4:e2b", "fake/scripted")
DEFAULT_MODEL = "ollama/gemma4:e2b"
_PREFIX = "CYGNUSFORGE_LLM_"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_CONTEXT_TOKENS = 32768


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./cygnusforge.db"
    secret_key: str = ""
    llm_models: tuple[str, ...] = DEFAULT_MODELS
    llm_default_model: str = DEFAULT_MODEL
    provider_base_urls: dict[str, str] = field(default_factory=dict)
    provider_api_keys: dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"
    llm_context_tokens: int = DEFAULT_CONTEXT_TOKENS


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _per_provider(env: Mapping[str, str], suffix: str) -> dict[str, str]:
    """`CYGNUSFORGE_LLM_<NAME>_<SUFFIX>` settings, keyed by the lowercase provider name."""
    return {
        key[len(_PREFIX) : -len(suffix)].lower(): value
        for key, value in env.items()
        if key.startswith(_PREFIX)
        and key.endswith(suffix)
        and len(key) > len(_PREFIX) + len(suffix)
    }


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    secret = env.get("CYGNUSFORGE_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("CYGNUSFORGE_SECRET_KEY is required")
    default_model = env.get("CYGNUSFORGE_LLM_DEFAULT_MODEL", DEFAULT_MODEL).strip()
    models = _csv(env.get("CYGNUSFORGE_LLM_PROVIDERS", ",".join(DEFAULT_MODELS)))
    # The default is always offered, so it never has to be listed twice.
    if default_model not in models:
        models = (default_model, *models)
    log_level = env.get("CYGNUSFORGE_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in _LOG_LEVELS:
        raise RuntimeError(f"CYGNUSFORGE_LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    raw_context = env.get("CYGNUSFORGE_LLM_CONTEXT_TOKENS", str(DEFAULT_CONTEXT_TOKENS)).strip()
    try:
        context_tokens = int(raw_context)
    except ValueError:
        context_tokens = 0
    if context_tokens < 1:
        raise RuntimeError("CYGNUSFORGE_LLM_CONTEXT_TOKENS must be a positive whole number")
    return Settings(
        database_url=env.get("CYGNUSFORGE_DATABASE_URL", "sqlite:///./cygnusforge.db"),
        secret_key=secret,
        llm_models=models,
        llm_default_model=default_model,
        provider_base_urls=_per_provider(env, "_BASE_URL"),
        provider_api_keys=_per_provider(env, "_API_KEY"),
        log_level=log_level,
        llm_context_tokens=context_tokens,
    )
