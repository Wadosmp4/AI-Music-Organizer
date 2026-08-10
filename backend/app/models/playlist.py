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
    # Structured hard-gate rule (KTD10), e.g. {"genre": "rock"}. A rule
    # present is a hard gate: the description is not used for candidacy on
    # this playlist when a rule exists (KTD10).
    rule: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    # How this playlist entered this app's management: "proposal" (an
    # accepted AI suggestion), "custom" (the user's own name/description), or
    # "adopted" (a pre-existing YouTube playlist brought under management).
    # Lets the Onboarding screen keep each playlist in the UI section it
    # originated from (checked) across a remount, instead of every accepted
    # proposal/custom addition collapsing into one generic "added" list once
    # it becomes a real Playlist row. None for rows created before this field
    # existed -- the frontend treats that the same as "adopted".
    source: Optional[str] = Field(default=None, nullable=True)
