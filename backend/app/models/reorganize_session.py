"""Reorganize session data model (U1, KTD2).

A `ReorganizeSession` anchors one "Reorganize My Library" run: the snapshot
of liked-song video ids it was triggered against, its background clustering
progress, and its Finish & Apply progress/concurrency guard. `PlaylistProposal`
persists U2's progressive clustering output incrementally, one row per
accepted cluster per batch-merge, so the poll endpoint has something durable
to read instead of an in-memory dict.
"""

from typing import Any, Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field

from app.models.base import TimestampMixin

# KTD9: background clustering progress, polled by the frontend.
CLUSTERING_STATUSES = ("pending", "in_progress", "done", "stalled")

# KTD10: doubles as Finish & Apply's concurrent-apply guard -- a new apply
# trigger is rejected while a session's apply_status is "in_progress".
APPLY_STATUSES = ("idle", "in_progress")


class ReorganizeSession(TimestampMixin, table=True):
    __tablename__ = "reorganize_session"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    # Captured at trigger time (and merged with newly-liked video_ids on
    # reuse, KTD2) -- the full liked-songs library this session matches
    # against, independent of whatever's already been ingested.
    video_id_snapshot: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    clustering_status: str = Field(default="pending", nullable=False)
    apply_status: str = Field(default="idle", nullable=False)
    # Per-run succeeded/failed counts and failed item ids (R11), read by the
    # apply-status poll endpoint (U6).
    apply_last_result: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))


class PlaylistProposal(TimestampMixin, table=True):
    __tablename__ = "playlist_proposal"

    id: Optional[int] = Field(default=None, primary_key=True)
    reorganize_session_id: int = Field(
        foreign_key="reorganize_session.id", nullable=False, index=True
    )
    name: str
    theme: str
    song_count: int
