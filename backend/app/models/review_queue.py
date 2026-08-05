from typing import Any, Optional

from sqlalchemy import CheckConstraint, Column, JSON
from sqlmodel import Field

from app.models.base import TimestampMixin

# Mirrors the review-queue lifecycle: pending -> write_pending -> approved/moved,
# with rejected/stale as terminal states reached directly from pending.
ALLOWED_STATUSES = ("pending", "write_pending", "approved", "moved", "rejected", "stale")
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
    status: str = Field(default="pending", nullable=False, index=True)
    version: int = Field(default=1, nullable=False)
    confidence: Optional[float] = Field(default=None)
    explanation: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
