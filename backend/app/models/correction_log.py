from typing import Any, Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field

from app.models.base import TimestampMixin


class CorrectionLogEntry(TimestampMixin, table=True):
    """Append-only record of a review-time override, feeding future classification (KTD12/KTD20)."""

    __tablename__ = "correction_log_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    review_queue_item_id: int = Field(foreign_key="review_queue_item.id", nullable=False, index=True)
    original_playlist_id: Optional[int] = Field(default=None, foreign_key="playlist.id")
    corrected_playlist_id: Optional[int] = Field(default=None, foreign_key="playlist.id")
    context: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
