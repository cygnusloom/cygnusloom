"""Chat and credits text renders as markdown: formatted, escaped, and with bare URLs linked."""

from __future__ import annotations

from cygnusloom.web.rendering import render_markdown


def test_a_bare_url_becomes_a_link_with_the_url_as_its_text():
    html = str(render_markdown("Cairn: https://cairnrpg.com/second-edition/."))

    assert (
        '<a href="https://cairnrpg.com/second-edition/">https://cairnrpg.com/second-edition/</a>'
    ) in html
    assert "</a>.</p>" in html


def test_markdown_links_bold_and_line_breaks_still_render():
    html = str(render_markdown("**Cairn** is a game.\nRead [the site](https://cairnrpg.com)."))

    assert "<strong>Cairn</strong>" in html
    assert '<a href="https://cairnrpg.com">the site</a>' in html
    assert "<br" in html


def test_raw_html_stays_escaped():
    html = str(render_markdown("<script>alert('x')</script>"))

    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_an_unsafe_link_is_left_as_text():
    html = str(render_markdown("[click](javascript:alert(1))"))

    assert "<a" not in html
    assert "javascript:alert(1)" in html
