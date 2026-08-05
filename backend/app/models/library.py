from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime
from sqlmodel import Field

from app.models.base import TimestampMixin


class LibraryItem(TimestampMixin, table=True):
    """A single liked song, as reported by the YouTube Music / Data API integration."""

    __tablename__ = "library_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    video_id: str = Field(index=True, unique=True)
    title: str
    artist: str
    liked_at: Optional[str] = Field(default=None)
    # Set by the ingestion job (U4) when a diff pass no longer finds this song
    # in the liked list. Reviewed synchronously at approve/move time (KTD14) so
    # a race between a paused scheduler and a user action can't approve a song
    # that's already gone.
    removed_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
