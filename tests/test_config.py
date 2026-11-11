from __future__ import annotations

import pytest

from cygnusloom.config import load_settings

BASE = {"CYGNUSLOOM_SECRET_KEY": "test-secret-key-0123456789abcdef"}


def test_default_model_is_offered_even_when_not_listed():
    settings = load_settings(
        {
            **BASE,
            "CYGNUSLOOM_LLM_PROVIDERS": "deepseek/deepseek-flash",
            "CYGNUSLOOM_LLM_DEFAULT_MODEL": "ollama/gemma4:e2b",
        }
    )
    assert settings.llm_default_model == "ollama/gemma4:e2b"
    assert settings.llm_models == ("ollama/gemma4:e2b", "deepseek/deepseek-flash")


def test_default_model_alone_is_enough():
    settings = load_settings({**BASE, "CYGNUSLOOM_LLM_DEFAULT_MODEL": "deepseek/deepseek-flash"})
    assert settings.llm_models[0] == "deepseek/deepseek-flash"
    assert settings.llm_default_model == "deepseek/deepseek-flash"


def test_no_models_are_configured_by_default():
    settings = load_settings(BASE)
    assert settings.llm_models == ()
    assert settings.llm_default_model == ""


def test_first_environment_model_is_the_default_when_none_is_named():
    settings = load_settings({**BASE, "CYGNUSLOOM_LLM_PROVIDERS": "ollama/one, openai/two"})
    assert settings.llm_models == ("ollama/one", "openai/two")
    assert settings.llm_default_model == "ollama/one"


def test_secret_key_is_required():
    with pytest.raises(RuntimeError, match="CYGNUSLOOM_SECRET_KEY"):
        load_settings({})


def test_context_window_is_not_an_environment_setting():
    settings = load_settings({**BASE, "CYGNUSLOOM_LLM_CONTEXT_TOKENS": "8192"})
    assert not hasattr(settings, "llm_context_tokens")


def test_prompt_detail_modes_default_to_brief_tools_and_rules():
    settings = load_settings(BASE)
    assert settings.tool_schema_mode == "brief"
    assert settings.rule_info_mode == "brief"


def test_music_is_enabled_by_default_and_can_be_disabled():
    assert load_settings(BASE).disable_music is False
    assert load_settings({**BASE, "CYGNUSLOOM_DISABLE_MUSIC": "1"}).disable_music is True


@pytest.mark.parametrize("value", ["full", "compact", "summary", "brief"])
def test_tool_schema_mode_accepts_each_supported_value(value: str):
    assert (
        load_settings({**BASE, "CYGNUSLOOM_TOOL_SCHEMA_MODE": f" {value} "}).tool_schema_mode
        == value
    )


def test_an_unknown_tool_schema_mode_is_refused():
    with pytest.raises(RuntimeError, match="CYGNUSLOOM_TOOL_SCHEMA_MODE"):
        load_settings({**BASE, "CYGNUSLOOM_TOOL_SCHEMA_MODE": "tiny"})


@pytest.mark.parametrize("value", ["full", "brief", "index"])
def test_rule_info_mode_accepts_each_supported_value(value: str):
    assert (
        load_settings({**BASE, "CYGNUSLOOM_RULE_INFO_MODE": f" {value} "}).rule_info_mode == value
    )


def test_an_unknown_rule_info_mode_is_refused():
    with pytest.raises(RuntimeError, match="CYGNUSLOOM_RULE_INFO_MODE"):
        load_settings({**BASE, "CYGNUSLOOM_RULE_INFO_MODE": "footnotes"})


def test_test_login_is_off_by_default():
    assert load_settings(BASE).test_login is False


@pytest.mark.parametrize("value", ["1", " TRUE ", "yes", "on"])
def test_test_login_can_be_enabled(value: str):
    assert load_settings({**BASE, "CYGNUSLOOM_TEST_LOGIN": value}).test_login is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_test_login_stays_off_for_other_values(value: str):
    assert load_settings({**BASE, "CYGNUSLOOM_TEST_LOGIN": value}).test_login is False


def test_debug_panel_is_off_by_default():
    assert load_settings(BASE).debug_panel is False


@pytest.mark.parametrize("value", ["1", " TRUE ", "yes", "on"])
def test_debug_panel_can_be_enabled(value: str):
    assert load_settings({**BASE, "CYGNUSLOOM_DEBUG_PANEL": value}).debug_panel is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_debug_panel_stays_off_for_other_values(value: str):
    assert load_settings({**BASE, "CYGNUSLOOM_DEBUG_PANEL": value}).debug_panel is False


OIDC = {
    "CYGNUSLOOM_BASE_URL": "https://cygnusloom.example.com",
    "CYGNUSLOOM_OIDC_ISSUER": "https://idp.example.com/application/o/cygnusloom/",
    "CYGNUSLOOM_OIDC_CLIENT_ID": "cygnusloom",
    "CYGNUSLOOM_OIDC_CLIENT_SECRET": "secret",
}


def test_oidc_settings_are_read():
    settings = load_settings(
        {
            **BASE,
            **OIDC,
            "CYGNUSLOOM_OIDC_ADMIN_CLAIM": "realm_access.roles",
            "CYGNUSLOOM_OIDC_ADMIN_VALUES": "forge-admin, forge-owner",
        }
    )
    assert settings.oidc_issuer == "https://idp.example.com/application/o/cygnusloom"
    assert settings.oidc_admin_claim == "realm_access.roles"
    assert settings.oidc_admin_values == ("forge-admin", "forge-owner")
    assert settings.base_url == "https://cygnusloom.example.com"


def test_secure_cookies_follow_the_base_url():
    assert load_settings({**BASE, **OIDC}).secure_cookies is True
    assert (
        load_settings({**BASE, "CYGNUSLOOM_BASE_URL": "http://localhost:8000"}).secure_cookies
        is False
    )


def test_openid_scope_is_always_requested():
    settings = load_settings({**BASE, **OIDC, "CYGNUSLOOM_OIDC_SCOPES": "email profile"})
    assert settings.oidc_scopes.split()[0] == "openid"


def test_production_requires_oidc():
    with pytest.raises(RuntimeError, match="CYGNUSLOOM_OIDC_ISSUER"):
        load_settings({**BASE, "CYGNUSLOOM_ENVIRONMENT": "production"})


def test_production_rejects_test_login():
    with pytest.raises(RuntimeError, match="test login"):
        load_settings(
            {**BASE, **OIDC, "CYGNUSLOOM_ENVIRONMENT": "production", "CYGNUSLOOM_TEST_LOGIN": "1"}
        )


def test_production_requires_https():
    with pytest.raises(RuntimeError, match="https"):
        load_settings(
            {
                **BASE,
                **OIDC,
                "CYGNUSLOOM_BASE_URL": "http://cygnusloom.example.com",
                "CYGNUSLOOM_ENVIRONMENT": "production",
            }
        )


def test_production_requires_an_https_issuer():
    with pytest.raises(RuntimeError, match="OIDC_ISSUER must use https"):
        load_settings(
            {
                **BASE,
                **OIDC,
                "CYGNUSLOOM_ENVIRONMENT": "production",
                "CYGNUSLOOM_OIDC_ISSUER": "http://idp.example.com",
            }
        )


def test_production_requires_secure_cookies():
    with pytest.raises(RuntimeError, match="secure cookies"):
        load_settings(
            {
                **BASE,
                **OIDC,
                "CYGNUSLOOM_ENVIRONMENT": "production",
                "CYGNUSLOOM_SECURE_COOKIES": "0",
            }
        )


def test_test_login_admin_requires_test_login():
    with pytest.raises(RuntimeError, match="TEST_LOGIN_ADMIN"):
        load_settings({**BASE, "CYGNUSLOOM_TEST_LOGIN_ADMIN": "1"})


def test_unknown_environment_is_rejected():
    with pytest.raises(RuntimeError, match="CYGNUSLOOM_ENVIRONMENT"):
        load_settings({**BASE, "CYGNUSLOOM_ENVIRONMENT": "staging"})


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_session_hours_must_be_positive(value: str):
    with pytest.raises(RuntimeError, match="CYGNUSLOOM_SESSION_HOURS"):
        load_settings({**BASE, "CYGNUSLOOM_SESSION_HOURS": value})


def test_auto_create_schema_defaults_off_in_production():
    settings = load_settings({**BASE, **OIDC, "CYGNUSLOOM_ENVIRONMENT": "production"})
    assert settings.auto_create_schema is False
