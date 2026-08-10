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

# KTD9: background clustering progress, polled by the frontend. "cancelled"
# is a terminal, non-retryable outcome distinct from "done"/"stalled" -- a
# cancelled session is never reused by get_open_for_user (see
# ReorganizeSessionRepository._is_unresolved), so the next trigger always
# starts a fresh one.
CLUSTERING_STATUSES = ("pending", "in_progress", "done", "stalled", "cancelled")

# KTD10: doubles as Finish & Apply's concurrent-apply guard -- a new apply
# trigger is rejected while a session's apply_status is "in_progress".
APPLY_STATUSES = ("idle", "in_progress")

# Background matching progress (U4 follow-up), same shape as
# clustering_status: matching now runs as a server-side background task
# (mirroring clustering) instead of a frontend-driven "call one batch, wait,
# call again" loop, so a page switch or closed tab no longer abandons it
# mid-run.
MATCHING_STATUSES = ("idle", "in_progress", "done")


class ReorganizeSession(TimestampMixin, table=True):
    __tablename__ = "reorganize_session"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    # Captured at trigger time (and merged with newly-liked video_ids on
    # reuse, KTD2) -- the full liked-songs library this session matches
    # against, independent of whatever's already been ingested.
    video_id_snapshot: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    clustering_status: str = Field(default="pending", nullable=False)
    # Live enrichment progress during background clustering (U2 follow-up):
    # how many of this session's tracks have gone through genre lookup
    # so far, out of len(video_id_snapshot) -- the poll endpoint uses this
    # for a real percentage instead of clustering_status alone, which used
    # to leave the whole (often slowest) enrichment phase invisible.
    enriched_count: int = Field(default=0, nullable=False)
    matching_status: str = Field(default="idle", nullable=False)
    # Live matching progress: how many of this session's tracks have been
    # classified against candidate playlists so far, out of
    # len(video_id_snapshot) -- same idea as enriched_count, for the phase
    # that used to run invisibly as a frontend-driven batch loop.
    matched_count: int = Field(default=0, nullable=False)
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
