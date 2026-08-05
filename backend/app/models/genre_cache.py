from typing import Optional

from sqlmodel import Field

from app.models.base import TimestampMixin


class GenreCacheEntry(TimestampMixin, table=True):
    """DB-backed replacement for lastfm.py's file cache (KTD23).

    Not user-scoped: an artist's genre tag is external, shared reference
    data, not something one user owns — every user benefits from the same
    cached lookup rather than each re-querying Last.fm independently.
    """

    __tablename__ = "genre_cache_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    artist: str = Field(index=True, unique=True)
    genre: Optional[str] = Field(default=None)
