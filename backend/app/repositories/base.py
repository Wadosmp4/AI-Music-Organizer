from typing import TypeVar

from sqlmodel import Session, SQLModel

T = TypeVar("T", bound=SQLModel)


def save(session: Session, obj: T) -> T:
    """The add/commit/refresh sequence every repository's create()/upsert() uses."""
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj
