from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from app.core.config import get_settings


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    if not get_settings().database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache
def get_engine():
    # One Engine (and its connection pool) for the process lifetime — settings
    # are themselves cached (get_settings), so there's nothing to re-read per
    # call. Un-cached, every request built a fresh pool from scratch, which
    # would mean a new TCP+auth handshake per request once this cuts over to
    # Postgres (KTD2).
    return create_engine(get_settings().database_url)


def get_session():
    engine = get_engine()
    with Session(engine) as session:
        yield session
