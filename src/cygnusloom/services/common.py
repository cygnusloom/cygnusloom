from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlmodel import Session


class NotFound(Exception):
    """The requested item does not exist or is not visible to the caller."""


class Conflict(Exception):
    """The request conflicts with existing state, such as a duplicate account."""


class InvalidInput(Exception):
    """A submitted value failed validation. The message is safe to show the user."""


def whole_number(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidInput(f"{label} must be a whole number from {low} to {high}")
    return value


def bounded_text(value: object, label: str, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise InvalidInput(f"{label} must be 1 to {limit} characters")
    return text


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as db:
        yield db
