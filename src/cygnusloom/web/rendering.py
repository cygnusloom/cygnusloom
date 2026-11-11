from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from cygnusloom import SOURCE_URL, WEBSITE_URL, __version__, content_credits
from cygnusloom.auth.deps import current_user, get_csrf, is_admin_user
from cygnusloom.auth.oidc import account_management_url
from cygnusloom.services.accounts import decode_roles
from cygnusloom.web.icons import icon_path, item_icon_id, place_icon_path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals.update(
    icon_path=icon_path, place_icon_path=place_icon_path, item_icon_id=item_icon_id
)

# Raw HTML in messages stays escaped; single newlines become line breaks for chat text, and a
# bare URL, such as a credit's source link, becomes an anchor even without link text. The
# commonmark preset leaves the linkify rules off even with the option set, so enable them.
_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": True})
_markdown.enable("linkify")


def render_markdown(text: str) -> Markup:
    """Render chat text as markdown. Raw HTML is escaped, so the result is safe to inline."""
    return Markup(_markdown.render(text))


@lru_cache(maxsize=32)
def _sheet_link_pattern(items: tuple[tuple[str, str], ...]) -> re.Pattern[str]:
    """One alternation for every linkable name, longest first so a longer name wins."""
    return re.compile(
        r"(?<![0-9A-Za-z_])("
        + "|".join(re.escape(name) for name, _ in items)
        + r")(?![0-9A-Za-z_])",
        re.IGNORECASE,
    )


def link_compendium(text: str, links: Mapping[str, str] | None) -> Markup:
    """Escape a line, then link every compendium name it mentions. Only already-escaped names
    are matched, and the anchors are escaped too, so the result is safe to inline."""
    if not links:
        return escape(text)
    by_name = {
        str(escape(name)).casefold(): str(escape(anchor))
        for name, anchor in links.items()
        if name and anchor
    }
    if not by_name:
        return escape(text)
    items = tuple(sorted(by_name.items(), key=lambda pair: len(pair[0]), reverse=True))
    pattern = _sheet_link_pattern(items)

    def replace(match: re.Match[str]) -> str:
        anchor = by_name.get(match.group(0).casefold())
        if anchor is None:
            return match.group(0)
        return f'<a href="#{anchor}" data-view-link="compendium">{match.group(0)}</a>'

    return Markup(pattern.sub(replace, str(escape(text))))


templates.env.filters["markdown"] = render_markdown
templates.env.filters["roles"] = decode_roles
templates.env.filters["compendium_links"] = link_compendium


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    """Render a page with the signed-in user (if any), its role check, and the CSRF token."""
    user = current_user(request)
    settings = request.app.state.settings
    values = {
        "user": user,
        "csrf_token": get_csrf(request),
        "is_admin": is_admin_user(request, user),
        "flash": request.session.pop("flash", None),
        "step_up_done": request.session.pop("step_up_done", False),
        "oidc_account_url": settings.oidc_account_url
        or account_management_url(settings.oidc_issuer),
        "single_player": settings.single_player,
        "music_default_enabled": not settings.disable_music,
        "data_dir": settings.data_dir,
        "app_version": __version__,
        "website_url": WEBSITE_URL,
        "source_url": SOURCE_URL,
        "credits": content_credits(),
        **context,
    }
    return templates.TemplateResponse(request, name, values, status_code=status_code)


def render_fragment(request: Request, name: str, **context: Any) -> Response:
    """Render a page fragment that the page can swap in place.

    It takes only the values its partials need: a fragment has no shell, flash, or account
    state, and rendering it must not consume a one-time flash meant for the next page load."""
    values = {
        "csrf_token": get_csrf(request),
        "single_player": request.app.state.settings.single_player,
        "music_default_enabled": not request.app.state.settings.disable_music,
        **context,
    }
    return templates.TemplateResponse(
        request,
        name,
        values,
        headers={"Cache-Control": "no-store"},
    )
