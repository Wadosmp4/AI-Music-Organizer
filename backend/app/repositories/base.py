from typing import TypeVar

from sqlmodel import Session, SQLModel

from app.models.base import utcnow

T = TypeVar("T", bound=SQLModel)


def save(session: Session, obj: T) -> T:
    """The add/commit/refresh sequence every repository's create()/upsert() uses.

    Bumps `updated_at` on every call, not just construction: TimestampMixin's
    `default_factory=utcnow` only fires when a Python object is first
    instantiated, not on subsequent mutate-then-save calls -- so without this,
    updated_at silently never changes after a row's first insert. Found via
    ReorganizeSessionRepository's "stalled" detection (KTD9), which reads
    `reorganize_session.updated_at` as an activity signal: it was comparing
    against a timestamp frozen at row creation, unable to tell a session that
    was still legitimately working from one that had gone quiet.
    """
    if hasattr(obj, "updated_at"):
        obj.updated_at = utcnow()
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj
