"""On-demand ingestion check (U4, F1, R10).

No background scheduler (KTD5, session-settled: user-directed) — this runs
once per call, invoked by `POST /api/v1/ingestion/check`. Diffs the current
liked songs against the persisted membership index (LibraryItem's unique
video_id, KTD7) instead of recomputing the whole library. The existing
backlog is processed as a bounded-batch-per-call backfill (KTD21) gated on
U9's onboarding selection having completed (F5); steady-state checks are
suspended until the first backfill pass finishes, so the two never race the
same backlog songs.
"""

from dataclasses import dataclass

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.ytmusic_client import track_artist
from app.models.library import LibraryItem
from app.models.review_queue import ReviewQueueItem
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError
from app.repositories.user_repository import UserRepository
from app.services.classification import CandidatePlaylist, ClassificationService

# Bounded per call so a single request can't burst GetSongBPM's 3,000/hour
# ceiling (KTD21) — the caller repeats the check until backfill_complete.
BACKFILL_BATCH_SIZE = 50

_ACTIVE_QUEUE_STATUSES = {"pending", "write_pending"}


@dataclass
class IngestionCheckResult:
    ran: bool  # False when gated (onboarding not complete yet, F5)
    mode: str  # "waiting_for_onboarding" | "backfill" | "steady_state"
    new_songs_found: int
    queue_items_created: int
    songs_marked_removed: int
    backfill_complete: bool  # only meaningful when mode == "backfill"


def run_ingestion_check(
    music_client: MusicServiceClient,
    classification_service: ClassificationService,
    library_repository: LibraryRepository,
    playlist_repository: PlaylistRepository,
    review_queue_repository: ReviewQueueRepository,
    user_repository: UserRepository,
    user_id: int,
) -> IngestionCheckResult:
    user = user_repository.get(user_id)
    if user is None or user.onboarding_completed_at is None:
        return IngestionCheckResult(
            ran=False,
            mode="waiting_for_onboarding",
            new_songs_found=0,
            queue_items_created=0,
            songs_marked_removed=0,
            backfill_complete=False,
        )

    is_backfill = user.backfill_completed_at is None
    mode = "backfill" if is_backfill else "steady_state"

    try:
        liked_songs = music_client.get_liked_songs()
    except Exception as exc:
        dependency_health_store.set_status(
            "youtube_detection",
            DependencyStatus.DEGRADED,
            f"ingestion check failed to fetch liked songs: {exc}",
        )
        return IngestionCheckResult(
            ran=True,
            mode=mode,
            new_songs_found=0,
            queue_items_created=0,
            songs_marked_removed=0,
            backfill_complete=False,
        )
    dependency_health_store.set_status("youtube_detection", DependencyStatus.OK)

    existing_items = library_repository.list_for_user(user_id)
    existing_video_ids = {item.video_id for item in existing_items}
    liked_video_ids = {s["videoId"] for s in liked_songs if s.get("videoId")}

    # Snapshotted once, up front — the staleness-marking CAS below uses each
    # queue item's version as of THIS snapshot, so a concurrent user action
    # that changes a row between this snapshot and the write is a real,
    # detectable conflict (KTD19), not a silent read-fresh-and-succeed.
    queue_items_snapshot = review_queue_repository.list_for_user(user_id)

    songs_marked_removed = _mark_removed_songs(
        existing_items, liked_video_ids, queue_items_snapshot, library_repository, review_queue_repository
    )

    all_new_songs = [
        s for s in liked_songs if s.get("videoId") and s["videoId"] not in existing_video_ids
    ]
    limit = BACKFILL_BATCH_SIZE if is_backfill else None
    new_songs = all_new_songs[:limit] if limit is not None else all_new_songs

    candidates = [
        CandidatePlaylist.from_playlist(p) for p in playlist_repository.list_for_user(user_id)
    ]

    queue_items_created = 0
    for song in new_songs:
        library_item = library_repository.create(
            LibraryItem(
                user_id=user_id,
                video_id=song["videoId"],
                title=song.get("title", ""),
                artist=track_artist(song),
            )
        )
        try:
            result = classification_service.classify_track(song, candidates, user_id=user_id)
        except Exception:
            # KTD18: this song's failure never blocks the rest of the check —
            # it stays unclassified and the next check call retries it.
            continue

        if result.playlist_id is None:
            continue

        review_queue_repository.create(
            ReviewQueueItem(
                user_id=user_id,
                library_item_id=library_item.id,
                playlist_id=result.playlist_id,
                confidence=result.confidence,
                explanation=result.as_explanation_dict(),
            )
        )
        queue_items_created += 1

    backfill_complete = False
    if is_backfill and len(all_new_songs) <= (limit or 0):
        user_repository.mark_backfill_completed(user_id)
        backfill_complete = True

    return IngestionCheckResult(
        ran=True,
        mode=mode,
        new_songs_found=len(new_songs),
        queue_items_created=queue_items_created,
        songs_marked_removed=songs_marked_removed,
        backfill_complete=backfill_complete,
    )


def _mark_removed_songs(
    existing_items: list[LibraryItem],
    liked_video_ids: set[str],
    queue_items_snapshot: list[ReviewQueueItem],
    library_repository: LibraryRepository,
    review_queue_repository: ReviewQueueRepository,
) -> int:
    """A library item no longer in the current liked list gets removed_at set
    (KTD14), and any of its active review_queue items get CAS-updated to
    stale using their version as of `queue_items_snapshot` — never a silent
    overwrite if a concurrent user action already changed that row's version
    in the meantime (KTD19)."""
    active_queue_items_by_library_item_id: dict[int, list[ReviewQueueItem]] = {}
    for queue_item in queue_items_snapshot:
        if queue_item.status in _ACTIVE_QUEUE_STATUSES:
            active_queue_items_by_library_item_id.setdefault(queue_item.library_item_id, []).append(
                queue_item
            )

    marked = 0
    for item in existing_items:
        if item.video_id in liked_video_ids or item.removed_at is not None:
            continue
        library_repository.mark_removed(item.id)
        marked += 1
        for queue_item in active_queue_items_by_library_item_id.get(item.id, []):
            try:
                review_queue_repository.update(queue_item.id, queue_item.version, status="stale")
            except VersionConflictError:
                # A concurrent user action (e.g. approve) already changed this
                # row — never silently overwritten (KTD19); leave it as-is.
                continue
    return marked
