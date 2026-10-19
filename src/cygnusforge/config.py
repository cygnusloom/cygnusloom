from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_MODELS = ("ollama/gemma4:e2b", "fake/scripted")
DEFAULT_MODEL = "ollama/gemma4:e2b"
_PREFIX = "CYGNUSFORGE_LLM_"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./cygnusforge.db"
    secret_key: str = ""
    llm_models: tuple[str, ...] = DEFAULT_MODELS
    llm_default_model: str = DEFAULT_MODEL
    provider_base_urls: dict[str, str] = field(default_factory=dict)
    provider_api_keys: dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"


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
    models = _csv(env.get("CYGNUSFORGE_LLM_PROVIDERS", ",".join(DEFAULT_MODELS)))
    default_model = env.get("CYGNUSFORGE_LLM_DEFAULT_MODEL", DEFAULT_MODEL)
    if default_model not in models:
        raise RuntimeError("CYGNUSFORGE_LLM_DEFAULT_MODEL must be one of CYGNUSFORGE_LLM_PROVIDERS")
    log_level = env.get("CYGNUSFORGE_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in _LOG_LEVELS:
        raise RuntimeError(f"CYGNUSFORGE_LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    return Settings(
        database_url=env.get("CYGNUSFORGE_DATABASE_URL", "sqlite:///./cygnusforge.db"),
        secret_key=secret,
        llm_models=models,
        llm_default_model=default_model,
        provider_base_urls=_per_provider(env, "_BASE_URL"),
        provider_api_keys=_per_provider(env, "_API_KEY"),
        log_level=log_level,
    )
