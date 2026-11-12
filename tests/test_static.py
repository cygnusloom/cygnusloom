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
