"""Run the web server on a throwaway SQLite database that is deleted when the server stops."""

from __future__ import annotations

import os
import tempfile

import uvicorn


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cygnusforge-") as tmp:
        os.environ["CYGNUSFORGE_DATABASE_URL"] = f"sqlite:///{tmp}/cygnusforge.db"
        os.environ.setdefault("CYGNUSFORGE_LLM_PROVIDERS", "fake/scripted")
        os.environ.setdefault("CYGNUSFORGE_LLM_DEFAULT_MODEL", "fake/scripted")
        uvicorn.run("cygnusforge.web.app:app_from_env", factory=True, port=8000)


if __name__ == "__main__":
    main()
