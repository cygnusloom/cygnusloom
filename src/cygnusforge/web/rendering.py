from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from cygnusforge.auth.deps import current_user, get_csrf

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    """Render a page with the signed-in user (if any) and the CSRF token for its forms."""
    values = {"user": current_user(request), "csrf_token": get_csrf(request), **context}
    return templates.TemplateResponse(request, name, values, status_code=status_code)
