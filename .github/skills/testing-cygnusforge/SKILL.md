---
name: testing-cygnusforge
description: Run Cygnus Forge tests two ways. Scripted tests use pytest with the fake model's __FAKE__ directive. UI tests start a live server and drive it with the browser tools. Use when asked to test, verify, or validate the app, or after changing routes, templates, services, or LLM providers.
---

# Testing Cygnus Forge

Two layers. Use the scripted layer for logic and the UI layer for anything a user sees.

## 1. Scripted tests (pytest)

```sh
uv run pytest -q
uv run ruff format --check
uv run ruff check
```

- Tests use the in-process app (`create_app` in `tests/conftest.py`) with a `FakeProvider`.
- The autouse fixture `block_live_models` blocks real httpx calls. A test that reaches the network fails with `LiveNetworkBlocked`. Stub the provider instead: `app.state.providers = lambda model_id: (Provider(), model_id)`.
- Fixtures: `client` (one browser-like cookie jar), `client_factory` (several users), `fake`, `settings`. Helpers `register`, `login`, and `csrf_token` are in `conftest.py`.
- Run one file or test with `uv run pytest tests/test_campaigns.py -q` or `-k name`.

### Scripting the fake model

`fake/scripted` reads a directive from a message. The prefix is `__FAKE__:` and the rest must be strict JSON with quoted keys:

```text
__FAKE__:{"turns": [
  {"text": "Hello."},
  {"text": "Rolling.", "tool_calls": [{"name": "roll", "arguments": {"sides": 20}, "output": "14"}]},
  {"error": "upstream exploded"}
]}
```

- Each assistant turn consumes the next entry in `turns`.
- `text` is the reply. `tool_calls` render as `<tool_call name="...">args</tool_call> <tool_output>...</tool_output>`.
- `error` makes the turn fail with a 502. Nothing is saved. The same entry is retried until a new directive is sent.
- A newer directive replaces the older one. When the script runs out, the model echoes your last message.

Use the directive in tests to script multi-turn conversations, tool output, and failures. Keep it in the test body, not in fixtures, so each test reads on its own.

## 2. UI tests (live server + browser tools)

Use these for flows that involve real pages: forms, redirects, the model dropdown, the error banner, and CSS rendering.

### Start the server

Bind to all interfaces so the browser can reach it. The browser cannot reach `127.0.0.1` or `localhost` in this environment.

```sh
rm -f /tmp/cf-ui.db
CYGNUSFORGE_DATABASE_URL=sqlite:////tmp/cf-ui.db \
  timeout 900 uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env \
  --host 0.0.0.0 --port 8765 > /tmp/cf-ui.log 2>&1 &
sleep 6
hostname -I            # first address is the container IP, e.g. 172.17.0.3
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/login   # expect 200
```

- Always use a fresh `/tmp` database. An old `cygnusforge.db` with the previous `campaign` schema breaks inserts.
- `--env-file .env` supplies the live DeepSeek config. Never print `.env` or `.devcontainer/devcontainer.env`. Their values are secrets.
- Use `timeout` so a forgotten server exits on its own.
- Use the URL `http://<container-ip>:8765`.

### Drive the browser

Load the browser tools first with `tool_search_tool` (for example "open browser page, run playwright code, screenshot page"). They are deferred.

1. `openBrowserPage` with `forceNew: true` and the container URL. Keep the returned `pageId`.
2. `runPlaywrightCode` with `pageId` for each flow. Use `timeoutMs` of 30000 or more for live model calls.
3. `screenshotPage` to check layout. Use it once per main page, not on every step.

Selectors that work:

| Element | Selector |
| --- | --- |
| Email / password | `input[name=email]`, `input[name=password]` |
| Register / log in submit | `form[action="/register"] button[type=submit]`, `form[action="/login"] button[type=submit]` |
| New campaign name | `input[name=name]` |
| Chat message | `textarea[name=content]` |
| Model dropdown | `select[name=model]` (use `page.selectOption`) |
| Send | `form.composer button[type=submit]` |
| Logout | `form[action="/logout"] button` |
| Error banner | `[role=alert]` |
| Messages | `.message` |

Scope selectors to the form. The header has a logout button too.

### Known pitfalls

- A successful send redirects to `/campaigns/<id>?model=...`. Wait on that URL with `waitForURL(/model=/)`.
- A failed send returns a 400 or 502 page at `/campaigns/<id>/messages`. It does not redirect. Waiting for a URL change here times out. Wait for `[role=alert]` instead.
- Fresh-user flow: register once, then use the same campaign for the rest of the test.

### Standard UI run

1. Start the server. Confirm the 200.
2. Register `ui@example.com` with a test password. Create a campaign.
3. Send a live DeepSeek message on the default model. Expect a reply, and the `?model=` redirect keeps the dropdown on that model.
4. Switch the dropdown to `fake/scripted`. Send a directive with a text turn, a tool-call turn, and an error turn. Check the text, the tag rendering, and the `[role=alert]` banner. Check that the draft stays in the textarea.
5. Log out. Log in with a wrong password and check the `Email or password is incorrect.` banner.
6. Screenshot the campaign page.

Use a live DeepSeek turn only when the change touches providers or the chat path. Scripted runs cover everything else. A live run needs `CYGNUSFORGE_LLM_DEEPSEEK_API_KEY` in `.env`. Without it, the send shows an error banner instead of a reply.

### Teardown

```sh
ps -eo pid,args | grep "[8]765"     # note the numeric PIDs
kill <pid> <pid> <pid>               # numeric PIDs only
rm -f /tmp/cf-ui.db /tmp/cf-ui.log
```

The shell `kill` only accepts numeric PIDs. Do not use `pkill` or `killall`. Check that nothing is left on port 8765.

## Before reporting done

- All scripted checks pass: pytest, ruff format check, ruff check.
- The UI run covers the flow you changed, with at least one screenshot.
- The server is stopped and `/tmp` files are removed.
- Nothing in `.env` or `.devcontainer/devcontainer.env` was printed or committed.
