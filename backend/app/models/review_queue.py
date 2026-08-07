from typing import Any, Optional

from sqlalchemy import CheckConstraint, Column, JSON
from sqlmodel import Field

from app.models.base import TimestampMixin

# Mirrors the review-queue lifecycle: pending -> write_pending -> approved/moved,
# with rejected/stale as terminal states reached directly from pending. A
# session-tagged item (reorganize_session_id is not None) instead goes
# pending -> approved_pending_apply -- a real decided state, just not yet
# written to YouTube (KTD1, KTD3) -- until Finish & Apply picks it up and
# drives it through write_pending -> approved/moved like any other item.
ALLOWED_STATUSES = (
    "pending",
    "write_pending",
    "approved",
    "moved",
    "rejected",
    "stale",
    "approved_pending_apply",
)
_ALLOWED_STATUSES_SQL = ", ".join(f"'{status}'" for status in ALLOWED_STATUSES)


class ReviewQueueItem(TimestampMixin, table=True):
    __tablename__ = "review_queue_item"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_ALLOWED_STATUSES_SQL})",
            name="ck_review_queue_item_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    library_item_id: int = Field(foreign_key="library_item.id", nullable=False, index=True)
    playlist_id: Optional[int] = Field(default=None, foreign_key="playlist.id", index=True)
    # Set once, at row-creation time, by session-scoped matching (U4) --
    # never re-evaluated afterward (KTD1). None means this item was created
    # by the ordinary lightweight ingestion loop and always writes
    # immediately on approve/move (R12).
    reorganize_session_id: Optional[int] = Field(
        default=None, foreign_key="reorganize_session.id", index=True
    )
    status: str = Field(default="pending", nullable=False, index=True)
    version: int = Field(default=1, nullable=False)
    confidence: Optional[float] = Field(default=None)
    explanation: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
