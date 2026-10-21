"""Run the web server on a throwaway SQLite database that is deleted when the server stops."""

from __future__ import annotations

import os
import tempfile

import uvicorn


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cygnusforge-") as tmp:
        os.environ["CYGNUSFORGE_DATABASE_URL"] = f"sqlite:///{tmp}/cygnusforge.db"
        uvicorn.run("cygnusforge.web.app:app_from_env", factory=True, port=8000)


if __name__ == "__main__":
    main()
