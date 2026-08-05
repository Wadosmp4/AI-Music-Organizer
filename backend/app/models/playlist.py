from typing import Optional

from sqlmodel import Field

from app.models.base import TimestampMixin


class Playlist(TimestampMixin, table=True):
    __tablename__ = "playlist"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    name: str
    youtube_playlist_id: Optional[str] = Field(default=None, index=True)
    description: Optional[str] = Field(default=None)
    rule: Optional[str] = Field(default=None)
