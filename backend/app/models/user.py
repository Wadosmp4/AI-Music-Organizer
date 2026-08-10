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
    # Background ingestion-check progress (steady-state/backfill batch loop,
    # same shape as ReorganizeSession.matching_status/matched_count): "idle" |
    # "in_progress" | "done". A single persistent row (not per-run, unlike
    # ReorganizeSession) since there's only ever one ingestion run active for
    # this user at a time -- ingestion_status doubles as that concurrency
    # guard, the same way ReorganizeSession.matching_status does per session.
    ingestion_status: str = Field(default="idle", nullable=False)
    ingestion_processed_count: int = Field(default=0, nullable=False)
    ingestion_total_count: int = Field(default=0, nullable=False)
    # Background onboarding-proposals generation progress (embed+HDBSCAN
    # clustering + genre enrichment), same idle/in_progress/done shape --
    # a single persistent row since there's only ever one proposals run
    # active for this user at a time; proposals_status doubles as that
    # concurrency guard.
    proposals_status: str = Field(default="idle", nullable=False)
    proposals_processed_count: int = Field(default=0, nullable=False)
    proposals_total_count: int = Field(default=0, nullable=False)
