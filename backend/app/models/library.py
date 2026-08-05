from typing import Optional

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
