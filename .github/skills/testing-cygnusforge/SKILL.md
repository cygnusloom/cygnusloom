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
- Bind the server to `0.0.0.0` and use the container's address (`hostname -I`). The browser cannot reach `localhost` or `127.0.0.1` here.
- Load the browser tools with `tool_search_tool` before using them. Open the page with `openBrowserPage` and `forceNew: true`, drive it with `runPlaywrightCode`, and check layout with `screenshotPage`.
- Scope selectors to the form or region under test, so generic buttons elsewhere on the page are not matched.
- Load config from the project's gitignored env file, and never print its values.

## Done

- Stop any server you started. Identify it by its numeric PID, since `kill` accepts only numeric PIDs.
- Remove the temporary database and log files.
