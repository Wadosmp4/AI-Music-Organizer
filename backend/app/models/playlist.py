from typing import Any, Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field

from app.models.base import TimestampMixin


class Playlist(TimestampMixin, table=True):
    __tablename__ = "playlist"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    name: str
    youtube_playlist_id: Optional[str] = Field(default=None, index=True)
    description: Optional[str] = Field(default=None)
    # Structured hard-gate rule (KTD10), e.g. {"genre": "rock", "bpm_min": 120, "bpm_max": 160}.
    # A rule present is a hard gate: the description is not used for candidacy on
    # this playlist when a rule exists (KTD10).
    rule: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
