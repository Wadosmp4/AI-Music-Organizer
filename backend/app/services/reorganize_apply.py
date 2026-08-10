"""Finish & Apply (U6, R9, R10, R11, KTD6, KTD9, KTD10).

Creates any playlist selected during Reorganize that doesn't yet have a
`youtube_playlist_id` (KTD6's deferred create), then writes every
`approved_pending_apply` item belonging to the session to YouTube,
best-effort: one item's failure never blocks the rest (R11). Naturally
idempotent and partial-apply-friendly (R10) -- a re-trigger only processes
whatever is still `approved_pending_apply`. Runs as a background task (same
own-DB-session pattern as U2's clustering runner) with progress persisted on
`ReorganizeSession.apply_last_result` (KTD9) so the poll endpoint has
something durable to read; `ReorganizeSession.apply_status` doubles as the
concurrent-apply guard (KTD10).
"""

from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Optional

from sqlmodel import Session

from app.core.db import get_engine
from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import MusicServiceClient
from app.jobs.ingestion import _mark_removed_songs
from app.models.base import utcnow
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError

# KTD10: a write_pending row older than this (a crash mid-write -- an
# ordinary single-request write path never takes this long) is reverted to
# its pre-write status (approved_pending_apply) at the start of every apply
# run, rather than left stuck forever.
WRITE_PENDING_STALE_THRESHOLD = timedelta(minutes=2)


class ReorganizeSessionNotFoundError(Exception):
    pass


class ApplyAlreadyInProgressError(Exception):
    """KTD10: a session's apply_status is already "in_progress" -- a new
    trigger is rejected rather than racing the running one."""


@dataclass
class ApplyResult:
    succeeded: int
    failed: int
    failed_item_ids: list[int]
    remaining: int


def trigger_apply(
    reorganize_session_repository: ReorganizeSessionRepository, reorganize_session_id: int, user_id: int
):
    """Synchronous pre-flight for the apply endpoint: validates the session
    and claims the concurrent-apply guard (KTD10) before the background
    task starts, so a rapid double-click can't ever get past this check."""
    reorganize_session = reorganize_session_repository.get(reorganize_session_id)
    if reorganize_session is None or reorganize_session.user_id != user_id:
        raise ReorganizeSessionNotFoundError(f"reorganize session {reorganize_session_id} not found")
    if reorganize_session.apply_status == "in_progress":
        raise ApplyAlreadyInProgressError(
            f"reorganize session {reorganize_session_id} already has an apply in progress"
        )
    reorganize_session.apply_status = "in_progress"
    return reorganize_session_repository.update(reorganize_session)


def _as_utc(dt):
    # SQLite doesn't persist tzinfo on DateTime(timezone=True) columns --
    # round-tripped datetimes come back naive.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _revert_stale_write_pending(
    review_queue_repository: ReviewQueueRepository, reorganize_session_id: int, user_id: int
) -> None:
    threshold = utcnow() - WRITE_PENDING_STALE_THRESHOLD
    for item in review_queue_repository.list_for_user(user_id):
        if item.reorganize_session_id != reorganize_session_id or item.status != "write_pending":
            continue
        if _as_utc(item.updated_at) >= threshold:
            continue
        try:
            review_queue_repository.update(item.id, item.version, status="approved_pending_apply")
        except VersionConflictError:
            continue  # a concurrent update already resolved this row


def _reconcile_unliked_songs(
    music_client: MusicServiceClient,
    library_repository: LibraryRepository,
    review_queue_repository: ReviewQueueRepository,
    reorganize_session,
) -> int:
    try:
        liked_songs = music_client.get_liked_songs()
    except Exception:
        # KTD18-style: the apply still proceeds against whatever's currently
        # approved_pending_apply rather than blocking on this fetch.
        return 0
    liked_video_ids = {s["videoId"] for s in liked_songs if s.get("videoId")}

    snapshot_ids = set(reorganize_session.video_id_snapshot)
    existing_items = [
        item
        for item in library_repository.list_for_user(reorganize_session.user_id)
        if item.video_id in snapshot_ids
    ]
    queue_items_snapshot = [
        item
        for item in review_queue_repository.list_for_user(reorganize_session.user_id)
        if item.reorganize_session_id == reorganize_session.id
    ]
    return _mark_removed_songs(
        existing_items, liked_video_ids, queue_items_snapshot, library_repository, review_queue_repository
    )


def _write_item(
    review_queue_repository: ReviewQueueRepository,
    library_repository: LibraryRepository,
    music_client: MusicServiceClient,
    item,
    youtube_playlist_id: str,
    existing_video_ids: set[str],
) -> bool:
    """Mirrors ReviewQueueService.approve()'s write_pending -> external call
    -> CAS-to-final-status sequence (KTD-shared machinery), but hardcoded
    for the apply path: these items are already known session-tagged and
    approved_pending_apply, so there's no session-branch check to make.

    `existing_video_ids` is this playlist's current YouTube membership,
    fetched once per playlist by the caller (not here) -- if the song is
    already in there (e.g. added outside this app, or by an earlier partial
    apply run that succeeded on YouTube but didn't get to record it before a
    crash), the write is skipped rather than creating a real duplicate track
    on the playlist; the item is still marked approved either way, since the
    end state -- the song correctly placed in the playlist -- is the same.
    """
    library_item = library_repository.get(item.library_item_id)
    if library_item is None:
        return False

    try:
        pending_item = review_queue_repository.update(item.id, item.version, status="write_pending")
    except VersionConflictError:
        return False

    if library_item.video_id in existing_video_ids:
        try:
            review_queue_repository.update(pending_item.id, pending_item.version, status="approved")
        except VersionConflictError:
            pass
        return True

    try:
        music_client.add_playlist_items(youtube_playlist_id, [library_item.video_id])
    except Exception as exc:
        auth_status_store.set_write_status(AuthStatus.NEEDS_RECONNECT, f"apply write failed: {exc}")
        try:
            review_queue_repository.update(
                pending_item.id, pending_item.version, status="approved_pending_apply"
            )
        except VersionConflictError:
            pass
        return False

    try:
        review_queue_repository.update(pending_item.id, pending_item.version, status="approved")
    except VersionConflictError:
        # The write to YouTube genuinely succeeded but a concurrent update
        # (e.g. the ordinary ingestion check's staleness pass) already
        # claimed this row -- logged as diagnosable elsewhere in this
        # codebase's equivalent path (ReviewQueueService); here it's simply
        # not double-counted as a failure, since the write itself worked.
        pass
    return True


def _apply_approved_items(
    music_client: MusicServiceClient,
    playlist_repository: PlaylistRepository,
    library_repository: LibraryRepository,
    review_queue_repository: ReviewQueueRepository,
    reorganize_session,
) -> ApplyResult:
    items = [
        item
        for item in review_queue_repository.list_for_user(reorganize_session.user_id)
        if item.reorganize_session_id == reorganize_session.id and item.status == "approved_pending_apply"
    ]
    playlist_ids = {item.playlist_id for item in items if item.playlist_id is not None}

    # KTD6: create each missing playlist's real YouTube counterpart exactly
    # once per run, memoized here, regardless of how many items target it.
    playlist_youtube_ids: dict[int, Optional[str]] = {}
    # This playlist's current YouTube membership, fetched once per playlist
    # (not once per item) -- amortizes the cost of the duplicate-prevention
    # check in _write_item across every item that targets the same playlist.
    playlist_video_ids: dict[int, set[str]] = {}
    for playlist_id in playlist_ids:
        playlist = playlist_repository.get(playlist_id)
        if playlist is None:
            playlist_youtube_ids[playlist_id] = None
            continue
        if playlist.youtube_playlist_id:
            playlist_youtube_ids[playlist_id] = playlist.youtube_playlist_id
        else:
            try:
                youtube_playlist_id = music_client.create_playlist(playlist.name, playlist.description or "")
            except Exception:
                playlist_youtube_ids[playlist_id] = None  # this playlist's items stay pending, reported failed
                continue
            playlist_repository.set_youtube_playlist_id(playlist_id, youtube_playlist_id)
            playlist_youtube_ids[playlist_id] = youtube_playlist_id

        youtube_playlist_id = playlist_youtube_ids[playlist_id]
        try:
            tracks = music_client.get_playlist_tracks(youtube_playlist_id)
            playlist_video_ids[playlist_id] = {
                t["videoId"] for t in tracks if t.get("videoId")
            }
        except Exception:
            # Fail open: if we can't confirm current membership, don't block
            # the write over it -- this check is a safety net on top of the
            # DB-level dedup (active_pairs_for_user), not the only guard.
            playlist_video_ids[playlist_id] = set()

    succeeded = 0
    failed = 0
    failed_item_ids: list[int] = []
    for item in items:
        youtube_playlist_id = (
            playlist_youtube_ids.get(item.playlist_id) if item.playlist_id is not None else None
        )
        if not youtube_playlist_id:
            failed += 1
            failed_item_ids.append(item.id)
            continue
        existing_video_ids = playlist_video_ids.get(item.playlist_id, set())
        if _write_item(
            review_queue_repository, library_repository, music_client, item, youtube_playlist_id, existing_video_ids
        ):
            succeeded += 1
        else:
            failed += 1
            failed_item_ids.append(item.id)

    remaining = sum(
        1
        for item in review_queue_repository.list_for_user(reorganize_session.user_id)
        if item.reorganize_session_id == reorganize_session.id and item.status == "approved_pending_apply"
    )

    return ApplyResult(succeeded=succeeded, failed=failed, failed_item_ids=failed_item_ids, remaining=remaining)


def run_finish_and_apply(
    reorganize_session_id: int, user_id: int, music_client: MusicServiceClient, engine=None
) -> None:
    """Background task (KTD9): opens its own DB session (same pattern as
    U2's `run_reorganize_clustering`). `engine` defaults to the process-wide
    engine; tests pass their isolated test engine explicitly.
    """
    engine = engine or get_engine()
    with Session(engine) as db_session:
        reorganize_session_repo = ReorganizeSessionRepository(db_session)
        library_repo = LibraryRepository(db_session)
        playlist_repo = PlaylistRepository(db_session)
        review_queue_repo = ReviewQueueRepository(db_session)

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is None:
            return

        _revert_stale_write_pending(review_queue_repo, reorganize_session_id, user_id)
        _reconcile_unliked_songs(music_client, library_repo, review_queue_repo, reorganize_session)

        result = _apply_approved_items(
            music_client, playlist_repo, library_repo, review_queue_repo, reorganize_session
        )

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is not None:
            reorganize_session.apply_status = "idle"
            reorganize_session.apply_last_result = {
                "succeeded": result.succeeded,
                "failed": result.failed,
                "failed_item_ids": result.failed_item_ids,
                "remaining": result.remaining,
            }
            reorganize_session_repo.update(reorganize_session)
