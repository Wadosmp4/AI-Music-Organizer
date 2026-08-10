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

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.db import get_engine
from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.youtube_data_api_client import YouTubeDataApiClient, track_artist
from app.models.library import LibraryItem
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError
from app.repositories.user_repository import UserRepository
from app.services.classification import ClassificationService, build_candidate, classify_tracks_concurrently
from app.services.genre_lookup import GenreLookupService

# Bounded per call so one request doesn't do unbounded classification work
# (a live LLM/description-match call per song) in one go (KTD21) — the
# caller repeats the check until backfill_complete. Kept small (rather than
# e.g. 50) so run_ingestion_check_to_completion's per-batch progress write
# is visible to the poll endpoint more often during a large backlog run.
BACKFILL_BATCH_SIZE = 20

_ACTIVE_QUEUE_STATUSES = {"pending", "write_pending", "approved_pending_apply"}

# A review_queue_item in one of these statuses reflects a song already
# written to a real YouTube playlist (ReviewQueueService.approve/move), or a
# real decided-but-not-yet-written session-scoped outcome
# (approved_pending_apply, KTD3) -- reset_backlog leaves these alone so a
# redo-the-backfill request never re-adds a song that's already been
# organized, and never silently discards an in-flight reorganize decision
# either.
_COMMITTED_QUEUE_STATUSES = {"approved", "moved", "approved_pending_apply"}


@dataclass
class IngestionCheckResult:
    ran: bool  # False when gated (onboarding not complete yet, F5)
    mode: str  # "waiting_for_onboarding" | "backfill" | "steady_state"
    new_songs_found: int
    queue_items_created: int
    songs_marked_removed: int
    backfill_complete: bool  # only meaningful when mode == "backfill"
    # Total new songs pending this run, before this call's batch cap (i.e.
    # len(all_new_songs)) -- lets a caller looping this function capture a
    # stable "out of how many" denominator from its first call, the same way
    # Reorganize's total_count is captured once from its own full snapshot.
    total_new_songs_found: int = 0


def run_ingestion_check(
    music_client: MusicServiceClient,
    classification_service: ClassificationService,
    library_repository: LibraryRepository,
    playlist_repository: PlaylistRepository,
    review_queue_repository: ReviewQueueRepository,
    user_repository: UserRepository,
    user_id: int,
    engine=None,
    candidates=None,
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

    # Every (library_item, playlist) pair that already has an active or
    # committed candidate row -- mirrors reorganize_matching.py's own dedup
    # (KTD8), so a retry/race here can't create a second pending candidate
    # for a pair that's already queued, approved, or written.
    active_pairs = review_queue_repository.active_pairs_for_user(user_id)

    all_new_songs = [
        s for s in liked_songs if s.get("videoId") and s["videoId"] not in existing_video_ids
    ]
    # Bounded unconditionally, not just during backfill: without a scheduler
    # (KTD5), a user who returns after time away and likes many songs at once
    # would otherwise hit this same steady-state check with an unbounded
    # batch, defeating the rate-limit protection this cap exists for.
    limit = BACKFILL_BATCH_SIZE
    new_songs = all_new_songs[:limit]

    # `candidates` lets a caller looping this function (e.g.
    # run_ingestion_check_to_completion) build the candidate playlists' real
    # YouTube content once per run instead of every batch -- omitted (the
    # default, and every existing test call site), this rebuilds them here
    # exactly as before.
    if candidates is None:
        candidates = [
            build_candidate(music_client, classification_service, playlist_repository, p)
            for p in playlist_repository.list_for_user(user_id)
        ]

    # `engine`, when given, fans these classify_track calls out across a
    # bounded thread pool instead of running them one at a time (see
    # classify_tracks_concurrently) -- each song's LLM description-match call
    # is independent, and this is the dominant per-batch cost. `None` (every
    # existing test call site) keeps the original single-threaded loop.
    classify_results = classify_tracks_concurrently(
        new_songs, candidates, classification_service, user_id, engine=engine
    )

    queue_items_created = 0
    for song, results in zip(new_songs, classify_results):
        if results is None:
            # KTD18: this song's failure never blocks the rest of the check.
            # The LibraryItem row is deliberately NOT created here — creating
            # it before classification, then skipping on failure, would add
            # the video_id to existing_video_ids and make the next check
            # believe this song was already handled, silently orphaning it
            # forever instead of actually retrying it as intended.
            continue

        try:
            library_item = library_repository.create(
                LibraryItem(
                    user_id=user_id,
                    video_id=song["videoId"],
                    title=song.get("title", ""),
                    artist=track_artist(song),
                )
            )
        except IntegrityError:
            # A concurrent check (e.g. a double-clicked retry) already
            # created this LibraryItem between this call's existing_video_ids
            # snapshot and this insert -- roll back the failed insert and
            # reuse the row the other call created instead of crashing the
            # whole check over one race (KTD18-style: one item's conflict
            # shouldn't block the rest).
            library_repository.session.rollback()
            existing = library_repository.get_by_video_id(song["videoId"])
            if existing is None:
                continue  # genuinely unexpected; skip this song this pass
            library_item = existing

        # One queue item per matched playlist -- a song can belong to more
        # than one -- or a single unassigned one when `results` is the
        # "no match" placeholder (playlist_id=None). Either way, an unmatched
        # song still gets a row: otherwise it's a LibraryItem with no
        # review_queue row at all, invisible everywhere and impossible to
        # ever manually assign to a playlist. It surfaces in the Review
        # Queue's "Unassigned" group instead, where it can be added to a
        # playlist like any other item.
        for result in results:
            pair = (library_item.id, result.playlist_id)
            if result.playlist_id is not None and pair in active_pairs:
                continue  # already an active/committed candidate there
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
            if result.playlist_id is not None:
                active_pairs.add(pair)

    backfill_complete = False
    if is_backfill and len(all_new_songs) <= limit:
        user_repository.mark_backfill_completed(user_id)
        backfill_complete = True

    return IngestionCheckResult(
        ran=True,
        mode=mode,
        new_songs_found=len(new_songs),
        queue_items_created=queue_items_created,
        songs_marked_removed=songs_marked_removed,
        backfill_complete=backfill_complete,
        total_new_songs_found=len(all_new_songs),
    )


class IngestionAlreadyInProgressError(Exception):
    """Raised by trigger_ingestion_check when this user's own ingestion run
    is already in progress -- mirrors MatchingAlreadyInProgressError /
    ApplyAlreadyInProgressError so a rapid double-click on "Load new songs"
    can't get two concurrent runs past this synchronous pre-flight check."""


def trigger_ingestion_check(user_repository: UserRepository, user_id: int) -> User:
    """Synchronous pre-flight for the ingestion-check trigger endpoint:
    claims the concurrent-run guard before the background task starts,
    mirroring reorganize_matching.trigger_matching."""
    user = user_repository.get(user_id)
    if user.ingestion_status == "in_progress":
        raise IngestionAlreadyInProgressError(
            f"ingestion check for user {user_id} already in progress"
        )
    return user_repository.set_ingestion_progress(user_id, status="in_progress", processed=0, total=0)


def run_ingestion_check_to_completion(
    user_id: int,
    engine=None,
    music_client=None,
    classification_service=None,
) -> None:
    """Background task (mirrors run_reorganize_matching's pattern): loops
    run_ingestion_check's own bounded batch until this run has nothing left
    to process, persisting progress after each batch so the poll endpoint
    has something durable to read. Replaces the old frontend-driven "click
    Load next 50 songs repeatedly" flow -- a user with a large backlog or a
    big burst of newly-liked songs no longer has to babysit it one batch at
    a time, and a page navigation or closed tab no longer abandons it
    mid-run.

    `engine`/`music_client`/`classification_service` follow the same
    injectable-for-tests pattern as run_reorganize_matching: leaving
    `classification_service` unset builds a real one reading real API keys
    from Settings, which isn't guarded by mocking `completion`/`requests.get`
    at this call site, so an unset value in a test can make a real, billed
    API call whenever real keys happen to be configured. Always pass it
    explicitly from a test.
    """
    engine = engine or get_engine()
    music_client = music_client or YouTubeDataApiClient()
    with Session(engine) as db_session:
        library_repository = LibraryRepository(db_session)
        playlist_repository = PlaylistRepository(db_session)
        review_queue_repository = ReviewQueueRepository(db_session)
        user_repository = UserRepository(db_session)
        # Per-song classification only runs concurrently (see
        # classify_tracks_concurrently) when this function built
        # classification_service itself -- guaranteed real, so a worker
        # thread can safely reconstruct an independent copy of it bound to
        # its own DB session. A caller-supplied one (every test, per this
        # docstring's own "always pass it explicitly from a test") might be
        # a mock; reconstructing from a mock's attributes would either raise
        # or silently ignore the mock's configured behavior, so
        # parallel_engine stays None in that case and run_ingestion_check
        # falls back to its original single-threaded loop.
        parallel_engine = None
        if classification_service is None:
            genre_lookup = GenreLookupService(db_session)
            classification_service = ClassificationService(
                genre_lookup, correction_log_repo=CorrectionLogRepository(db_session)
            )
            parallel_engine = engine

        # Built once for the whole run rather than every batch: each
        # candidate's real YouTube track list was previously refetched on
        # every single run_ingestion_check call, which for a large backlog
        # meant a full re-fetch of every managed playlist every ~20 songs --
        # the dominant per-batch cost. Slightly stale within one run if a
        # playlist's real content changes mid-run (e.g. the user manually
        # approves a Review Queue item while a big backfill is still going)
        # -- self-corrects on the next trigger, same tradeoff the reorganize
        # matching loop below now also takes.
        candidates = [
            build_candidate(music_client, classification_service, playlist_repository, p)
            for p in playlist_repository.list_for_user(user_id)
        ]

        processed = 0
        total_known = False
        while True:
            result = run_ingestion_check(
                music_client=music_client,
                classification_service=classification_service,
                library_repository=library_repository,
                playlist_repository=playlist_repository,
                review_queue_repository=review_queue_repository,
                user_repository=user_repository,
                user_id=user_id,
                engine=parallel_engine,
                candidates=candidates,
            )
            if not result.ran:
                break

            if not total_known:
                # Captured once, from the first batch's pre-cap count -- a
                # stable "out of how many" denominator for the whole run,
                # the same way Reorganize's total_count is fixed from its
                # own initial full-library snapshot.
                user_repository.set_ingestion_progress(user_id, total=result.total_new_songs_found)
                total_known = True
            processed += result.new_songs_found
            user_repository.set_ingestion_progress(user_id, processed=processed)

            if result.mode == "backfill":
                if result.backfill_complete:
                    break
            elif result.new_songs_found < BACKFILL_BATCH_SIZE:
                break
            if result.new_songs_found == 0:
                # Safety net: a batch that made no progress at all (e.g. a
                # persistent get_liked_songs failure) would otherwise loop
                # forever retrying the exact same call.
                break

        final_status = (
            "failed"
            if dependency_health_store.failed_with_no_progress("youtube_detection", processed > 0)
            else "done"
        )
        user_repository.set_ingestion_progress(user_id, status=final_status)


@dataclass
class BacklogResetResult:
    library_items_cleared: int


def reset_backlog(
    library_repository: LibraryRepository,
    review_queue_repository: ReviewQueueRepository,
    user_repository: UserRepository,
    user_id: int,
) -> BacklogResetResult:
    """Restarts backfill from the beginning of the user's liked list (a
    user-requested do-over, e.g. after adding new playlists partway through
    a backfill run that earlier batches never got a chance to match
    against). Songs already committed to a playlist (approved/moved --
    already written to YouTube) are left completely untouched; every other
    LibraryItem/review_queue_item is deleted so the next check treats those
    songs as new again and reclassifies them from scratch.
    """
    queue_items = review_queue_repository.list_for_user(user_id)
    committed_library_item_ids = {
        item.library_item_id for item in queue_items if item.status in _COMMITTED_QUEUE_STATUSES
    }
    queue_items_by_library_item_id: dict[int, list[ReviewQueueItem]] = {}
    for item in queue_items:
        queue_items_by_library_item_id.setdefault(item.library_item_id, []).append(item)

    cleared = 0
    for library_item in library_repository.list_for_user(user_id):
        if library_item.id in committed_library_item_ids:
            continue
        for queue_item in queue_items_by_library_item_id.get(library_item.id, []):
            review_queue_repository.delete(queue_item)
        library_repository.delete(library_item)
        cleared += 1

    user_repository.reset_backfill(user_id)
    return BacklogResetResult(library_items_cleared=cleared)


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
    in the meantime (KTD19).

    Captures each snapshot item's `(id, version)` as plain ints up front,
    rather than holding onto the ORM object and reading `.version` lazily
    later: `library_repository.mark_removed()` below calls `session.commit()`
    on the same Session, which by default expires every object in its
    identity map (not just the one just saved). A later `.version` attribute
    access on an expired snapshot object transparently triggers a fresh
    reload of the *current* DB row — silently replacing the intended stale
    snapshot value with the live one and defeating this exact CAS check.
    """
    active_versions_by_library_item_id: dict[int, list[tuple[int, int]]] = {}
    for queue_item in queue_items_snapshot:
        if queue_item.status in _ACTIVE_QUEUE_STATUSES:
            active_versions_by_library_item_id.setdefault(queue_item.library_item_id, []).append(
                (queue_item.id, queue_item.version)
            )

    marked = 0
    for item in existing_items:
        if item.video_id in liked_video_ids or item.removed_at is not None:
            continue
        library_repository.mark_removed(item.id)
        marked += 1
        for queue_item_id, queue_item_version in active_versions_by_library_item_id.get(item.id, []):
            try:
                review_queue_repository.update(queue_item_id, queue_item_version, status="stale")
            except VersionConflictError:
                # A concurrent user action (e.g. approve) already changed this
                # row — never silently overwritten (KTD19); leave it as-is.
                continue
    return marked
