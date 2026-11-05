"""Run the web server on a throwaway SQLite database that is deleted when the server stops."""

from __future__ import annotations

import os
import tempfile

import uvicorn


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cygnusloom-") as tmp:
        os.environ["CYGNUSLOOM_DATABASE_URL"] = f"sqlite:///{tmp}/cygnusloom.db"
        os.environ.setdefault("CYGNUSLOOM_LLM_PROVIDERS", "fake/scripted")
        os.environ.setdefault("CYGNUSLOOM_LLM_DEFAULT_MODEL", "fake/scripted")
        os.environ.setdefault("CYGNUSLOOM_TEST_LOGIN", "1")
        os.environ.setdefault("CYGNUSLOOM_TEST_LOGIN_ADMIN", "1")
        uvicorn.run("cygnusloom.web.app:app_from_env", factory=True, port=8000)


if __name__ == "__main__":
    main()
