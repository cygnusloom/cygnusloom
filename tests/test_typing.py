from __future__ import annotations

from cygnusloom.web import typing as typing_module
from cygnusloom.web.typing import TypingRegistry


def test_typing_hints_expire_and_can_be_stopped(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(typing_module.time, "monotonic", lambda: now[0])
    registry = TypingRegistry(seconds=5)

    registry.note(7, 2, "Ada")
    registry.note(7, 1, "Brenna")
    assert [(typer.user_id, typer.name) for typer in registry.active(7)] == [
        (2, "Ada"),
        (1, "Brenna"),
    ]

    registry.stop(7, 2)
    assert [(typer.user_id, typer.name) for typer in registry.active(7)] == [(1, "Brenna")]

    now[0] += 6
    assert registry.active(7) == ()


def test_typing_hints_are_kept_per_campaign():
    registry = TypingRegistry()

    registry.note(1, 3, "Ada")

    assert registry.active(2) == ()
    assert [typer.name for typer in registry.active(1)] == ["Ada"]


def test_a_blank_typing_name_falls_back_to_the_account():
    registry = TypingRegistry()

    registry.note(1, 4, "   ")

    assert [typer.name for typer in registry.active(1)] == ["account 4"]
