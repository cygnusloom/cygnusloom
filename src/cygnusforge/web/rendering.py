from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from cygnusforge.auth.deps import current_user, get_csrf
from cygnusforge.web.icons import icon_path, item_icon_id, place_icon_path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals.update(
    icon_path=icon_path, place_icon_path=place_icon_path, item_icon_id=item_icon_id
)

# Raw HTML in messages stays escaped; single newlines become line breaks for chat text.
_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True})


def render_markdown(text: str) -> Markup:
    """Render chat text as markdown. Raw HTML is escaped, so the result is safe to inline."""
    return Markup(_markdown.render(text))


templates.env.filters["markdown"] = render_markdown


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    """Render a page with the signed-in user (if any) and the CSRF token for its forms."""
    values = {"user": current_user(request), "csrf_token": get_csrf(request), **context}
    return templates.TemplateResponse(request, name, values, status_code=status_code)
