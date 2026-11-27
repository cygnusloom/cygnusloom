def test_static_revalidation_keeps_the_header(client):
    first = client.get("/static/creation.js")
    again = client.get("/static/creation.js", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"
