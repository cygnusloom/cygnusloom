from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_MODELS: tuple[str, ...] = ()
DEFAULT_MODEL = ""
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_OIDC_SCOPES = "openid email profile groups"
DEFAULT_ADMIN_VALUES = ("cygnusloom-admins",)
_PREFIX = "CYGNUSLOOM_LLM_"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_ENVIRONMENTS = ("development", "production")
TOOL_SCHEMA_MODES = ("full", "compact", "summary", "brief")
RULE_INFO_MODES = ("full", "brief", "index")
# The prompt budget a model starts with. Administrators can change it per model at runtime.
DEFAULT_MODEL_CONTEXT_TOKENS = 65536
DEFAULT_SESSION_HOURS = 7 * 24
DEFAULT_STEP_UP_MINUTES = 15
_TRUE_VALUES = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./cygnusloom.db"
    secret_key: str = ""
    llm_models: tuple[str, ...] = DEFAULT_MODELS
    llm_default_model: str = DEFAULT_MODEL
    provider_base_urls: dict[str, str] = field(default_factory=dict)
    provider_api_keys: dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"
    # How much of a tool schema reaches the provider. `full` is the authored schema;
    # `compact` drops nested documentation and long choice lists; `brief` also shortens the
    # top-level description. The engine always validates against the full schema.
    tool_schema_mode: str = "brief"
    # How much rule text the system prompt carries. `brief` gives each rule one line; `index`
    # gives only its id and relies on lookup_rule; `full` inlines every rule.
    rule_info_mode: str = "brief"
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
    single_player: bool = False
    data_dir: str = ""


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _per_provider(env: Mapping[str, str], suffix: str) -> dict[str, str]:
    """`CYGNUSLOOM_LLM_<NAME>_<SUFFIX>` settings, keyed by the lowercase provider name."""
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
    secret = env.get("CYGNUSLOOM_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("CYGNUSLOOM_SECRET_KEY is required")
    default_model = env.get("CYGNUSLOOM_LLM_DEFAULT_MODEL", DEFAULT_MODEL).strip()
    models = _csv(env.get("CYGNUSLOOM_LLM_PROVIDERS", ",".join(DEFAULT_MODELS)))
    # The default is always offered, so it never has to be listed twice. A development
    # setup that lists models but no default uses the first one.
    if default_model and default_model not in models:
        models = (default_model, *models)
    if not default_model and models:
        default_model = models[0]
    log_level = env.get("CYGNUSLOOM_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in _LOG_LEVELS:
        raise RuntimeError(f"CYGNUSLOOM_LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    tool_schema_mode = env.get("CYGNUSLOOM_TOOL_SCHEMA_MODE", "brief").strip().lower()
    if tool_schema_mode not in TOOL_SCHEMA_MODES:
        raise RuntimeError(
            "CYGNUSLOOM_TOOL_SCHEMA_MODE must be one of " + ", ".join(TOOL_SCHEMA_MODES)
        )
    rule_info_mode = env.get("CYGNUSLOOM_RULE_INFO_MODE", "brief").strip().lower()
    if rule_info_mode not in RULE_INFO_MODES:
        raise RuntimeError("CYGNUSLOOM_RULE_INFO_MODE must be one of " + ", ".join(RULE_INFO_MODES))
    environment = env.get("CYGNUSLOOM_ENVIRONMENT", "development").strip().lower()
    if environment not in _ENVIRONMENTS:
        raise RuntimeError(f"CYGNUSLOOM_ENVIRONMENT must be one of {', '.join(_ENVIRONMENTS)}")
    base_url = env.get("CYGNUSLOOM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError("CYGNUSLOOM_BASE_URL must start with http:// or https://")
    secure_raw = env.get("CYGNUSLOOM_SECURE_COOKIES", "").strip().lower()
    secure_cookies = base_url.startswith("https://") if not secure_raw else _bool(secure_raw)
    session_hours = _whole(env, "CYGNUSLOOM_SESSION_HOURS", DEFAULT_SESSION_HOURS, 1, 8760)
    step_up_minutes = _whole(env, "CYGNUSLOOM_STEP_UP_MINUTES", DEFAULT_STEP_UP_MINUTES, 0, 1440)
    oidc_issuer = env.get("CYGNUSLOOM_OIDC_ISSUER", "").strip().rstrip("/")
    oidc_client_id = env.get("CYGNUSLOOM_OIDC_CLIENT_ID", "").strip()
    oidc_client_secret = env.get("CYGNUSLOOM_OIDC_CLIENT_SECRET", "").strip()
    oidc_scopes = env.get("CYGNUSLOOM_OIDC_SCOPES", DEFAULT_OIDC_SCOPES).strip()
    if "openid" not in oidc_scopes.split():
        oidc_scopes = f"openid {oidc_scopes}".strip()
    oidc_admin_claim = env.get("CYGNUSLOOM_OIDC_ADMIN_CLAIM", "groups").strip()
    oidc_admin_values = _csv(
        env.get("CYGNUSLOOM_OIDC_ADMIN_VALUES", ",".join(DEFAULT_ADMIN_VALUES))
    )
    if not oidc_admin_claim:
        raise RuntimeError("CYGNUSLOOM_OIDC_ADMIN_CLAIM must not be empty")
    if not oidc_admin_values:
        raise RuntimeError("CYGNUSLOOM_OIDC_ADMIN_VALUES must name at least one role")
    test_login = _bool(env.get("CYGNUSLOOM_TEST_LOGIN"))
    test_login_admin = _bool(env.get("CYGNUSLOOM_TEST_LOGIN_ADMIN"))
    if test_login_admin and not test_login:
        raise RuntimeError("CYGNUSLOOM_TEST_LOGIN_ADMIN requires CYGNUSLOOM_TEST_LOGIN")
    single_player = _bool(env.get("CYGNUSLOOM_SINGLE_PLAYER"))
    if single_player:
        if environment == "production":
            raise RuntimeError("CYGNUSLOOM_SINGLE_PLAYER cannot run in production")
        if test_login or test_login_admin:
            raise RuntimeError("CYGNUSLOOM_SINGLE_PLAYER has its own local sign-in")
        if urlsplit(base_url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(
                "CYGNUSLOOM_BASE_URL must be a loopback address in single-player mode"
            )
    if environment == "production":
        if test_login or test_login_admin:
            raise RuntimeError("test login must be off in production")
        if not (oidc_issuer and oidc_client_id and oidc_client_secret):
            raise RuntimeError(
                "CYGNUSLOOM_OIDC_ISSUER, CYGNUSLOOM_OIDC_CLIENT_ID, and "
                "CYGNUSLOOM_OIDC_CLIENT_SECRET are required in production"
            )
        if not base_url.startswith("https://"):
            raise RuntimeError("CYGNUSLOOM_BASE_URL must use https in production")
        if not secure_cookies:
            raise RuntimeError("secure cookies must be on in production")
        if not oidc_issuer.startswith("https://"):
            raise RuntimeError("CYGNUSLOOM_OIDC_ISSUER must use https in production")
    auto_create = env.get(
        "CYGNUSLOOM_AUTO_CREATE_SCHEMA",
        "1" if environment == "development" or single_player else "0",
    )
    return Settings(
        database_url=env.get("CYGNUSLOOM_DATABASE_URL", "sqlite:///./cygnusloom.db"),
        secret_key=secret,
        llm_models=models,
        llm_default_model=default_model,
        provider_base_urls=_per_provider(env, "_BASE_URL"),
        provider_api_keys=_per_provider(env, "_API_KEY"),
        log_level=log_level,
        tool_schema_mode=tool_schema_mode,
        rule_info_mode=rule_info_mode,
        test_login=test_login,
        debug_panel=_bool(env.get("CYGNUSLOOM_DEBUG_PANEL")),
        environment=environment,
        base_url=base_url,
        oidc_issuer=oidc_issuer,
        oidc_client_id=oidc_client_id,
        oidc_client_secret=oidc_client_secret,
        oidc_scopes=oidc_scopes,
        oidc_name=env.get("CYGNUSLOOM_OIDC_NAME", "Single sign-on").strip() or "Single sign-on",
        oidc_admin_claim=oidc_admin_claim,
        oidc_admin_values=oidc_admin_values,
        oidc_account_url=env.get("CYGNUSLOOM_OIDC_ACCOUNT_URL", "").strip(),
        session_hours=session_hours,
        step_up_minutes=step_up_minutes,
        secure_cookies=secure_cookies,
        auto_create_schema=_bool(auto_create),
        test_login_admin=test_login_admin,
        single_player=single_player,
        data_dir=env.get("CYGNUSLOOM_DATA_DIR", "").strip(),
    )
