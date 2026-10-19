---
name: testing-cygnusforge
description: Run Cygnus Forge tests two ways. Scripted tests use pytest with the fake model's __FAKE__ directive. UI tests start a live server and drive it with the browser tools. Use when asked to test, verify, or validate the app, or after changing routes, templates, services, or LLM providers.
---

# Testing Cygnus Forge

## Automated (pytest)

- Setup: `uv sync`.
- Run: `uv run pytest`, plus `uv run ruff format --check` and `uv run ruff check`.
- Tests use the in-process app with the fake provider. Real network calls are blocked, so use `fake/scripted`.
- Script a conversation with a `__FAKE__:` directive in a message. It is strict JSON with quoted keys, and each assistant turn consumes the next entry in `turns`:

  ```text
  __FAKE__:{"turns": [{"text": "Hello."}, {"error": "upstream exploded"}]}
  ```

  Supported entries are `text`, `tool_calls`, and `error`. A newer directive replaces the older one.

## UI (live server + browser tools)

- Setup: the live model config lives in `.env` (gitignored). Use `--env-file .env`. Never print it.
- Start the server on a fresh `/tmp` database, bound to `0.0.0.0`, because the browser cannot reach `127.0.0.1` or `localhost`. Use the container IP from `hostname -I`, not localhost.
- Use a fresh database each run. An old `cygnusforge.db` can break inserts.
- Browser tools are deferred. Load them with `tool_search_tool` before calling them. Open the server URL with `openBrowserPage` (`forceNew: true`), then drive it with `runPlaywrightCode`, and check layout with `screenshotPage`.
- Scope selectors to the form, because the header has its own logout button.
- Stop the server when you're done, and remove the `/tmp` files.
