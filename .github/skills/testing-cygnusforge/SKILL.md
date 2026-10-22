---
name: testing-cygnusforge
description: Test a web app in two layers. Automated tests run with the project's test runner and use a fake or scripted LLM provider. UI tests start a live server and drive it with the browser tools. Use when asked to test, verify, or validate the app, or after changing routes, templates, services, or providers.
---

Generally you should test both the automated and UI aspects of the web app to ensure comprehensive coverage. Test using a real LLM if possible, using deepseek.

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
- Debug panel: set `CYGNUSFORGE_DEBUG_PANEL=1` to show a Debug tab in the campaign sidebar (off by default). It can make a player character invincible, meaning no HP or stat loss from attacks or outside harm, so a tester can keep the PC acting through a fight. Turning invincibility on also cancels any save the PC owes. The tab is a campaign-page tab, and the route returns 404 when the variable is off. Invincibility is hidden from the GM. The same tab has a "Set score" form that sets the player character's current HP or STR (`remaining`, at most the maximum). Use it to force a hit to 0 HP for a Scars or knock-out check. It is refused with 409 while a GM turn is running, so wait for `data-gm-state="waiting"` first. The form also records a hidden `combat.debug_stat_set` event.
- If multiple chats are working simultaneously, ensure each chat is isolated and does not interfere with the others. Use unique identifiers or separate browser contexts for each chat session. You may need to run the server on different ports for each session. Coordinate by writing to the .gitignored file .copilot_testing to record your current sessions; be sure to clean this file up afterwards.
- Always use your browser tools to wait for elements to appear or actions to complete, rather than relying on fixed delays. This ensures tests are more reliable and less prone to timing issues.
- Waiting for a GM turn: the campaign transcript (`[data-transcript]`) carries `data-gm-state`. It is `"busy"` while the GM is working on a turn, and `"waiting"` once the page is ready for player input. Clicking Send, or any other control that starts a turn, sets `"busy"` synchronously, and a turn that finishes or fails reloads or sets `"waiting"`. After starting a turn, wait with one `waitFor` on `[data-gm-state="waiting"]` on the page. Do not poll or sleep, and do not wait for `"busy"` first. A turn takes one to three minutes on DeepSeek, so use a long wait timeout.
- Do not treat old "The GM stopped" or "did not finish" text on the page as a completion signal. Use `data-gm-state`.

## Done

- Stop any server you started. Identify it by its numeric PID, since `kill` accepts only numeric PIDs.
- Remove the temporary database and log files.
