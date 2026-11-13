def test_static_revalidation_keeps_the_header(client):
    first = client.get("/static/creation.js")
    again = client.get("/static/creation.js", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


def test_the_markdown_renderer_is_served_for_streamed_replies(client):
    response = client.get("/static/markdown.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "window.renderMarkdown" in response.text


def test_a_campaign_opens_at_the_newest_message(client):
    """The transcript starts at the bottom and stays there when its layout settles."""
    script = client.get("/static/turn_stream.js").text

    assert "transcript.scrollTop = transcript.scrollHeight" in script
    assert 'window.addEventListener("pageshow"' in script
    assert "new ResizeObserver(keepBottom)" in script


def test_posting_forms_show_a_busy_button(client):
    """Every page carries the helper that turns a pressed submit button into a spinner."""
    script = client.get("/static/busy.js")

    assert script.status_code == 200
    assert "window.cygnusBusy" in script.text
    assert "dataset.busyLabel" in script.text
    assert "data-no-busy" in script.text
    assert '<script src="/static/busy.js"></script>' in client.get("/license").text
