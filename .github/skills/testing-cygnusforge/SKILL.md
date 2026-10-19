---
name: testing-cygnusforge
description: Test a web app in two layers. Automated tests run with the project's test runner and use a fake or scripted LLM provider. UI tests start a live server and drive it with the browser tools. Use when asked to test, verify, or validate the app, or after changing routes, templates, services, or providers.
---

# Testing a web app

## Automated tests

- Use the project's own test runner and linter. Find them in the project config (`pyproject.toml`, `package.json`, `Makefile`) rather than assuming.
- Never call a real LLM from automated tests. Use the fake or scripted provider, and block real network calls if the test setup supports it.
- For multi-turn behavior, script the replies with the fake provider's input format instead of mocking inside the code.

## UI tests

- Start the app locally on a fresh, throwaway database. Don't reuse an older one, since the schema may have changed.
- Set the database with `CYGNUSFORGE_DATABASE_URL`, for example `sqlite:////tmp/<dir>/test.db`. If it is unset, the app uses `./cygnusforge.db`, which is the user's local data. Never start a UI test server without setting it.
- Browser testing uses the container's IP address, not `localhost` or `127.0.0.1`. The browser cannot reach those; it shows `ERR_CONNECTION_REFUSED`.
  1. Get the address with `hostname -I` and take the first value, for example `172.17.0.3`.
  2. Start uvicorn with `--host 0.0.0.0` so it listens on that address, for example `uv run uvicorn --factory cygnusforge.web.app:app_from_env --host 0.0.0.0 --port 8765`.
  3. Check it with `curl -s -o /dev/null -w "%{http_code}" http://<container-ip>:<port>/register`, and expect `200`.
  4. Open every browser URL as `http://<container-ip>:<port>/...`. Don't reuse URLs opened on `localhost` or `127.0.0.1`.
- Load the browser tools with `tool_search_tool` before using them. Open the page with `openBrowserPage` and `forceNew: true`, drive it with `runPlaywrightCode`, and check layout with `screenshotPage`.
- Scope selectors to the form or region under test, so generic buttons elsewhere on the page are not matched.
- Load config from the project's gitignored env file, and never print its values.
- If multiple chats are working simultaneously, ensure each chat is isolated and does not interfere with the others. Use unique identifiers or separate browser contexts for each chat session. You may need to run the server on different ports for each session. Coordinate by writing to the .gitignored file .copilot_testing to record your current sessions; be sure to clean this file up afterwards.

## Done

- Stop any server you started. Identify it by its numeric PID, since `kill` accepts only numeric PIDs.
- Remove the temporary database and log files.
