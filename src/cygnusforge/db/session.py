from sqlalchemy import Engine
from sqlmodel import SQLModel, create_engine

import cygnusforge.db.models  # noqa: F401  (registers tables on SQLModel.metadata)


def make_engine(url: str) -> Engine:
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=args)


def create_schema(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
