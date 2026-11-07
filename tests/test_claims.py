from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cygnusloom.auth.claims import (
    OIDCError,
    claim_path,
    claim_values,
    claims_from_mapping,
    has_admin_role,
    roles_from_claims,
)


def test_claim_path_reads_nested_claims():
    claims = {"realm_access": {"roles": ["admin", "user"]}}
    assert claim_path(claims, "realm_access.roles") == ["admin", "user"]
    assert claim_path(claims, "missing.path") is None


def test_claim_values_accepts_one_string_or_a_list():
    assert claim_values("cygnusloom-admins") == ("cygnusloom-admins",)
    assert claim_values(["b", "a", "a"]) == ("a", "b")
    assert claim_values(None) == ()


def test_roles_follow_the_configured_claim():
    claims = {"groups": ["cygnusloom-admins", "readers"], "roles": ["ignored"]}
    assert roles_from_claims(claims, "groups") == ("cygnusloom-admins", "readers")
    assert roles_from_claims({"realm_access": {"roles": ["owner"]}}, "realm_access.roles") == (
        "owner",
    )


def test_admin_role_matching_is_case_insensitive():
    assert has_admin_role(("Cygnusloom-Admins",), ("cygnusloom-admins",))
    assert not has_admin_role(("readers",), ("cygnusloom-admins",))


def test_claims_require_the_configured_issuer():
    data = {"iss": "https://other.example.com", "sub": "abc"}
    with pytest.raises(OIDCError, match="different issuer"):
        claims_from_mapping(data, issuer="https://idp.example.com", admin_claim="groups")


def test_claims_require_a_subject():
    with pytest.raises(OIDCError, match="subject"):
        claims_from_mapping(
            {"iss": "https://idp.example.com"},
            issuer="https://idp.example.com",
            admin_claim="groups",
        )


def test_claims_map_email_name_and_roles():
    claims = claims_from_mapping(
        {
            "iss": "https://idp.example.com/",
            "sub": "abc",
            "email": "Ada@Example.com",
            "email_verified": "true",
            "name": "Ada Lovelace",
            "groups": ["cygnusloom-admins"],
            "auth_time": 1_700_000_000,
        },
        issuer="https://idp.example.com",
        admin_claim="groups",
    )
    assert claims.issuer == "https://idp.example.com"
    assert claims.email == "ada@example.com"
    assert claims.email_verified is True
    assert claims.display_name == "Ada Lovelace"
    assert claims.roles == ("cygnusloom-admins",)
    assert claims.auth_time == datetime.fromtimestamp(1_700_000_000, UTC)


def test_claims_do_not_use_email_as_a_display_name():
    claims = claims_from_mapping(
        {"iss": "https://idp.example.com", "sub": "abc", "email": "ada@example.com"},
        issuer="https://idp.example.com",
        admin_claim="groups",
    )
    assert claims.display_name == ""
    assert claims.email_verified is False


def test_claims_use_a_provider_name_but_not_an_email_shaped_one():
    named = claims_from_mapping(
        {
            "iss": "https://idp.example.com",
            "sub": "abc",
            "email": "ada@example.com",
            "name": "Ada Lovelace",
        },
        issuer="https://idp.example.com",
        admin_claim="groups",
    )
    assert named.display_name == "Ada Lovelace"

    email_shaped = claims_from_mapping(
        {
            "iss": "https://idp.example.com",
            "sub": "abc",
            "email": "ada@example.com",
            "preferred_username": "ada@example.com",
        },
        issuer="https://idp.example.com",
        admin_claim="groups",
    )
    assert email_shaped.display_name == ""

    username_fallback = claims_from_mapping(
        {
            "iss": "https://idp.example.com",
            "sub": "abc",
            "email": "ada@example.com",
            "name": "ada@example.com",
            "preferred_username": "ada",
        },
        issuer="https://idp.example.com",
        admin_claim="groups",
    )
    assert username_fallback.display_name == "ada"


def test_auth_time_defaults_to_now_when_missing():
    before = datetime.now(UTC)
    claims = claims_from_mapping(
        {"iss": "https://idp.example.com", "sub": "abc"},
        issuer="https://idp.example.com",
        admin_claim="groups",
    )
    assert claims.auth_time >= before
