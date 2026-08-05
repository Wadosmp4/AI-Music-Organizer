from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime
from sqlmodel import Field

from app.models.base import TimestampMixin


class User(TimestampMixin, table=True):
    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    display_name: str
    # Set once onboarding selection completes (U9/F5). U4's backfill gates on
    # this being non-null — the backfill classifies against the full set of
    # existing plus newly-created playlists, so it must not start until the
    # user has finished deciding what those playlists are.
    onboarding_completed_at: Optional[datetime] = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    # Set once the bounded backlog backfill (U4, KTD21) has processed every
    # already-liked song at least once. Steady-state on-demand checks stay
    # suspended (only bounded-batch backfill runs) until this is set, so the
    # two passes never race the same backlog songs.
    backfill_completed_at: Optional[datetime] = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
