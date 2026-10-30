from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_MODELS = ("ollama/gemma4:e2b", "fake/scripted")
DEFAULT_MODEL = "ollama/gemma4:e2b"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_OIDC_SCOPES = "openid email profile groups"
DEFAULT_ADMIN_VALUES = ("cygnusforge-admins",)
_PREFIX = "CYGNUSFORGE_LLM_"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_ENVIRONMENTS = ("development", "production")
DEFAULT_CONTEXT_TOKENS = 32768
DEFAULT_SESSION_HOURS = 7 * 24
DEFAULT_STEP_UP_MINUTES = 15
_TRUE_VALUES = ("1", "true", "yes", "on")


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
    test_login: bool = False
    debug_panel: bool = False
    environment: str = "development"
    base_url: str = DEFAULT_BASE_URL
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = DEFAULT_OIDC_SCOPES
    oidc_name: str = "Single sign-on"
    oidc_admin_claim: str = "groups"
    oidc_admin_values: tuple[str, ...] = DEFAULT_ADMIN_VALUES
    oidc_account_url: str = ""
    session_hours: int = DEFAULT_SESSION_HOURS
    step_up_minutes: int = DEFAULT_STEP_UP_MINUTES
    secure_cookies: bool = False
    auto_create_schema: bool = True
    test_login_admin: bool = False


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


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _whole(env: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    raw = env.get(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not low <= value <= high:
        raise RuntimeError(f"{key} must be a whole number from {low} to {high}")
    return value


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
    context_tokens = _whole(
        env, "CYGNUSFORGE_LLM_CONTEXT_TOKENS", DEFAULT_CONTEXT_TOKENS, 1, 1_000_000
    )
    environment = env.get("CYGNUSFORGE_ENVIRONMENT", "development").strip().lower()
    if environment not in _ENVIRONMENTS:
        raise RuntimeError(f"CYGNUSFORGE_ENVIRONMENT must be one of {', '.join(_ENVIRONMENTS)}")
    base_url = env.get("CYGNUSFORGE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError("CYGNUSFORGE_BASE_URL must start with http:// or https://")
    secure_raw = env.get("CYGNUSFORGE_SECURE_COOKIES", "").strip().lower()
    secure_cookies = base_url.startswith("https://") if not secure_raw else _bool(secure_raw)
    session_hours = _whole(env, "CYGNUSFORGE_SESSION_HOURS", DEFAULT_SESSION_HOURS, 1, 8760)
    step_up_minutes = _whole(env, "CYGNUSFORGE_STEP_UP_MINUTES", DEFAULT_STEP_UP_MINUTES, 0, 1440)
    oidc_issuer = env.get("CYGNUSFORGE_OIDC_ISSUER", "").strip().rstrip("/")
    oidc_client_id = env.get("CYGNUSFORGE_OIDC_CLIENT_ID", "").strip()
    oidc_client_secret = env.get("CYGNUSFORGE_OIDC_CLIENT_SECRET", "").strip()
    oidc_scopes = env.get("CYGNUSFORGE_OIDC_SCOPES", DEFAULT_OIDC_SCOPES).strip()
    if "openid" not in oidc_scopes.split():
        oidc_scopes = f"openid {oidc_scopes}".strip()
    oidc_admin_claim = env.get("CYGNUSFORGE_OIDC_ADMIN_CLAIM", "groups").strip()
    oidc_admin_values = _csv(
        env.get("CYGNUSFORGE_OIDC_ADMIN_VALUES", ",".join(DEFAULT_ADMIN_VALUES))
    )
    if not oidc_admin_claim:
        raise RuntimeError("CYGNUSFORGE_OIDC_ADMIN_CLAIM must not be empty")
    if not oidc_admin_values:
        raise RuntimeError("CYGNUSFORGE_OIDC_ADMIN_VALUES must name at least one role")
    test_login = _bool(env.get("CYGNUSFORGE_TEST_LOGIN"))
    test_login_admin = _bool(env.get("CYGNUSFORGE_TEST_LOGIN_ADMIN"))
    if test_login_admin and not test_login:
        raise RuntimeError("CYGNUSFORGE_TEST_LOGIN_ADMIN requires CYGNUSFORGE_TEST_LOGIN")
    if environment == "production":
        if test_login or test_login_admin:
            raise RuntimeError("test login must be off in production")
        if not (oidc_issuer and oidc_client_id and oidc_client_secret):
            raise RuntimeError(
                "CYGNUSFORGE_OIDC_ISSUER, CYGNUSFORGE_OIDC_CLIENT_ID, and "
                "CYGNUSFORGE_OIDC_CLIENT_SECRET are required in production"
            )
        if not base_url.startswith("https://"):
            raise RuntimeError("CYGNUSFORGE_BASE_URL must use https in production")
    auto_create = env.get(
        "CYGNUSFORGE_AUTO_CREATE_SCHEMA", "1" if environment == "development" else "0"
    )
    return Settings(
        database_url=env.get("CYGNUSFORGE_DATABASE_URL", "sqlite:///./cygnusforge.db"),
        secret_key=secret,
        llm_models=models,
        llm_default_model=default_model,
        provider_base_urls=_per_provider(env, "_BASE_URL"),
        provider_api_keys=_per_provider(env, "_API_KEY"),
        log_level=log_level,
        llm_context_tokens=context_tokens,
        test_login=test_login,
        debug_panel=_bool(env.get("CYGNUSFORGE_DEBUG_PANEL")),
        environment=environment,
        base_url=base_url,
        oidc_issuer=oidc_issuer,
        oidc_client_id=oidc_client_id,
        oidc_client_secret=oidc_client_secret,
        oidc_scopes=oidc_scopes,
        oidc_name=env.get("CYGNUSFORGE_OIDC_NAME", "Single sign-on").strip() or "Single sign-on",
        oidc_admin_claim=oidc_admin_claim,
        oidc_admin_values=oidc_admin_values,
        oidc_account_url=env.get("CYGNUSFORGE_OIDC_ACCOUNT_URL", "").strip(),
        session_hours=session_hours,
        step_up_minutes=step_up_minutes,
        secure_cookies=secure_cookies,
        auto_create_schema=_bool(auto_create),
        test_login_admin=test_login_admin,
    )
