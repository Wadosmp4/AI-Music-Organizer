import os
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
    test_engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(test_engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    from app import models  # noqa: F401

    SQLModel.metadata.create_all(test_engine)
    yield test_engine


@pytest.fixture()
def session(engine):
    with Session(engine) as session:
        yield session
