"""Helpers for reading submitted form fields."""

from __future__ import annotations

from starlette.datastructures import FormData


def form_text(form: FormData, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""
