from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cygnusloom.content import library
from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import REQUIRED_TABLES


@dataclass(frozen=True)
class ConfiguredRuleData:
    definition_type: str
    id: str
    value: int
    character_stats: tuple[str, ...]
    label: str


def configure_rule_data(data, character, label):
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContentError(f"{label}: 'value' must be a whole number")
    return ConfiguredRuleData(
        definition_type=data["type"],
        id=data["id"],
        value=value,
        character_stats=tuple(sorted(character.stats)),
        label=label,
    )


def return_no_rule_data(data, character, label):
    return None


def fail_to_configure(data, character, label):
    raise RuntimeError("invalid progression settings")


def fail_with_programming_error(data, character, label):
    raise AssertionError("configurator invariant failed")


non_callable_configurator = "not a function"


def _definition(kind: str, ident: str, **fields: Any) -> Definition:
    return Definition(kind, ident, {"type": kind, "id": ident, **fields})


def _rule_set(ident: str) -> Definition:
    return _definition(
        "rule_set",
        ident,
        name=f"{ident.title()} rules",
        description="A test ruleset.",
    )


def _rule_data(ident: str, **fields: Any) -> Definition:
    return _definition("rule_data", ident, **fields)


def _python_module(ident: str = "rules", module: str = "tests.test_rule_data") -> Definition:
    return _definition("python_module", ident, module=module)


def _tables() -> list[Definition]:
    return [_definition("table", table_id, options=["One"]) for table_id in REQUIRED_TABLES]


def _build(
    *rule_data: Definition,
    module: Definition | None = None,
) -> library.RuleSet:
    definitions = [_rule_set("testset"), *_tables()]
    if module is not None:
        definitions.append(module)
    definitions.extend(rule_data)
    return library._build_rule_set(definitions, "testset")


def _configured(ident: str, value: Any, **fields: Any) -> Definition:
    return _rule_data(
        ident,
        python_module="rules",
        configure="configure_rule_data",
        value=value,
        **fields,
    )


def test_rule_data_is_configured_during_ruleset_load_and_keeps_multiple_results():
    rule_set = _build(
        _configured("growth", 4),
        _configured("skills", 7),
        module=_python_module(),
    )

    assert rule_set.rule_data == {
        "growth": ConfiguredRuleData(
            definition_type="rule_data",
            id="growth",
            value=4,
            character_stats=(),
            label="testset: rule_data growth",
        ),
        "skills": ConfiguredRuleData(
            definition_type="rule_data",
            id="skills",
            value=7,
            character_stats=(),
            label="testset: rule_data skills",
        ),
    }


def test_rule_data_uses_existing_core_inheritance_and_override(monkeypatch):
    core = [
        _rule_set("core"),
        *_tables(),
        _python_module(),
        _configured("shared", 1),
        _configured("core_only", 2),
    ]
    own = [
        _rule_set("testset"),
        _configured("shared", 3),
        _configured("local_only", 4),
    ]
    monkeypatch.setattr(library, "read_definitions", lambda directory: own)

    rule_set = library._load_rule_set(Path("testset"), core)

    assert {key: value.value for key, value in rule_set.rule_data.items()} == {
        "shared": 3,
        "core_only": 2,
        "local_only": 4,
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"configure": "configure_rule_data"}, "'python_module' must be a non-empty string"),
        ({"python_module": "rules"}, "'configure' must be a non-empty string"),
    ],
)
def test_rule_data_requires_a_module_and_configurator(fields, message):
    with pytest.raises(ContentError, match=message):
        _build(
            _rule_data("bad", **fields),
            module=_python_module(),
        )


def test_rule_data_rejects_an_undeclared_module():
    with pytest.raises(
        ContentError,
        match="testset: rule_data growth: unknown python_module 'missing'",
    ):
        _build(
            _rule_data(
                "growth",
                python_module="missing",
                configure="configure_rule_data",
                value=1,
            ),
            module=_python_module(),
        )


def test_rule_data_reports_a_module_import_failure():
    with pytest.raises(
        ContentError,
        match="testset: python_module broken: cannot import 'tests.no_such_rule_data_module'",
    ):
        _build(
            _rule_data(
                "growth",
                python_module="broken",
                configure="configure_rule_data",
                value=1,
            ),
            module=_python_module("broken", "tests.no_such_rule_data_module"),
        )


def test_rule_data_rejects_a_non_callable_configurator():
    with pytest.raises(
        ContentError,
        match="configurator 'non_callable_configurator' is not callable in python_module 'rules'",
    ):
        _build(
            _rule_data(
                "growth",
                python_module="rules",
                configure="non_callable_configurator",
            ),
            module=_python_module(),
        )


def test_rule_data_rejects_a_none_result():
    with pytest.raises(ContentError, match="configurator 'return_no_rule_data'.*returned None"):
        _build(
            _rule_data(
                "growth",
                python_module="rules",
                configure="return_no_rule_data",
            ),
            module=_python_module(),
        )


def test_rule_data_preserves_configurator_content_errors():
    with pytest.raises(ContentError) as exc_info:
        _build(
            _configured("growth", "invalid"),
            module=_python_module(),
        )

    assert str(exc_info.value) == "testset: rule_data growth: 'value' must be a whole number"


def test_rule_data_wraps_runtime_configurator_failures_with_context_and_cause():
    with pytest.raises(
        ContentError,
        match="testset: rule_data growth: configurator 'fail_to_configure'.*invalid progression",
    ) as exc_info:
        _build(
            _rule_data(
                "growth",
                python_module="rules",
                configure="fail_to_configure",
            ),
            module=_python_module(),
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "invalid progression settings"


def test_rule_data_does_not_wrap_unexpected_configurator_errors():
    with pytest.raises(AssertionError, match="configurator invariant failed"):
        _build(
            _rule_data(
                "growth",
                python_module="rules",
                configure="fail_with_programming_error",
            ),
            module=_python_module(),
        )


def test_rule_data_requires_an_object_definition():
    definition = Definition("rule_data", "bad", None)
    with pytest.raises(
        ContentError, match="testset: rule_data bad: definition data must be an object"
    ):
        _build(definition, module=_python_module())
