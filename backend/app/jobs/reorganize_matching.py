"""Session-scoped full-library matching (U4, R6, R7, KTD8).

Mirrors `run_ingestion_check`'s batching shape (same batch size), but
iterates the reorganize session's `video_id_snapshot` instead of gating on
`existing_video_ids`: `run_ingestion_check` can only ever classify songs
brand-new to the library (its `existing_video_ids` gate, jobs/ingestion.py
lines 104-106), so it can never re-evaluate an already-ingested song against
a newly-selected playlist -- exactly what "match the full library" (R6)
requires. Stays a synchronous per-batch call like today's ingestion check
(KTD8) -- no new performance risk, since it does the same per-song
classification work at the same batch size.

"Already processed this session" (bounding repeated batch calls, and
avoiding endlessly re-evaluating a song that matches nothing) is tracked by
whether the library item already has at least one review_queue_item tagged
with this session's id -- once tagged, a song is done for this pass even if
it matched nothing (it still gets exactly one row, mirroring the lightweight
ingestion loop's "Unassigned" placeholder). A song can always be
re-evaluated by a *later*, different reorganize session (each session's tag
is distinct), which is what re-running reorganize (AE4) relies on.
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
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.classification import ClassificationService, build_candidate, classify_tracks_concurrently
from app.services.genre_lookup import GenreLookupService
from app.services.reorganize_apply import ReorganizeSessionNotFoundError

# Same shape as jobs/ingestion.py's BACKFILL_BATCH_SIZE (KTD8) -- bounded so a
# single request doesn't do unbounded classification work in one go, and kept
# small so per-batch progress is visible to the poll endpoint more often.
MATCHING_BATCH_SIZE = 20


class MatchingAlreadyInProgressError(Exception):
    """Raised by trigger_matching (U4 follow-up) when a session's own
    matching run is already in progress -- mirrors reorganize_apply's
    ApplyAlreadyInProgressError (KTD10) so a rapid double-click on "Finish
    setup" can't get two concurrent matching runs past this synchronous
    pre-flight check."""


def trigger_matching(reorganize_session_repository: ReorganizeSessionRepository, reorganize_session_id: int, user_id: int):
    """Synchronous pre-flight for the matching trigger endpoint: validates
    the session and claims the concurrent-matching guard before the
    background task starts, mirroring reorganize_apply.trigger_apply."""
    reorganize_session = reorganize_session_repository.get(reorganize_session_id)
    if reorganize_session is None or reorganize_session.user_id != user_id:
        raise ReorganizeSessionNotFoundError(f"reorganize session {reorganize_session_id} not found")
    if reorganize_session.matching_status == "in_progress":
        raise MatchingAlreadyInProgressError(
            f"reorganize session {reorganize_session_id} already has matching in progress"
        )
    reorganize_session.matching_status = "in_progress"
    return reorganize_session_repository.update(reorganize_session)


@dataclass
class ReorganizeMatchingResult:
    ran: bool
    processed: int
    queue_items_created: int
    matching_complete: bool  # True once every snapshot video_id has been considered this session


def run_reorganize_matching_batch(
    music_client: MusicServiceClient,
    classification_service: ClassificationService,
    library_repository: LibraryRepository,
    playlist_repository: PlaylistRepository,
    review_queue_repository: ReviewQueueRepository,
    reorganize_session_repository: ReorganizeSessionRepository,
    user_id: int,
    reorganize_session_id: int,
    engine=None,
    candidates=None,
) -> ReorganizeMatchingResult:
    reorganize_session = reorganize_session_repository.get(reorganize_session_id)
    if reorganize_session is None or reorganize_session.user_id != user_id:
        return ReorganizeMatchingResult(
            ran=False, processed=0, queue_items_created=0, matching_complete=False
        )

    try:
        liked_songs = music_client.get_liked_songs()
    except Exception as exc:
        # R14/KTD2: mirrors jobs/ingestion.py's run_ingestion_check -- shares
        # the same "youtube_detection" health key since it's the same
        # underlying dependency, so run_reorganize_matching below can tell a
        # genuine fetch failure apart from a batch that legitimately had
        # nothing left to process.
        dependency_health_store.set_status(
            "youtube_detection",
            DependencyStatus.DEGRADED,
            f"reorganize matching failed to fetch liked songs: {exc}",
        )
        return ReorganizeMatchingResult(
            ran=False, processed=0, queue_items_created=0, matching_complete=False
        )
    dependency_health_store.set_status("youtube_detection", DependencyStatus.OK)
    songs_by_video_id = {s["videoId"]: s for s in liked_songs if s.get("videoId")}

    existing_by_video_id = {
        item.video_id: item for item in library_repository.list_for_user(user_id)
    }
    session_tagged_library_item_ids = {
        item.library_item_id
        for item in review_queue_repository.list_for_user(user_id)
        if item.reorganize_session_id == reorganize_session_id
    }
    active_pairs = review_queue_repository.active_pairs_for_user(user_id)

    def _already_processed(video_id: str) -> bool:
        library_item = existing_by_video_id.get(video_id)
        return library_item is not None and library_item.id in session_tagged_library_item_ids

    pending_video_ids = [
        vid for vid in reorganize_session.video_id_snapshot if not _already_processed(vid)
    ]
    batch = pending_video_ids[:MATCHING_BATCH_SIZE]

    # `candidates` lets a caller looping this function (run_reorganize_matching)
    # build the candidate playlists' real YouTube content once per run
    # instead of every batch -- omitted (the default, and every existing
    # test call site), this rebuilds them here exactly as before.
    if candidates is None:
        candidates = [
            build_candidate(music_client, classification_service, playlist_repository, p)
            for p in playlist_repository.list_for_user(user_id)
        ]

    # Phase 1 (sequential DB writes): resolve or create each batch song's
    # LibraryItem row -- classify_track below is read-mostly and safe to
    # parallelize (see classify_tracks_concurrently), but a row insert must
    # stay serialized against this function's own session.
    batch_songs = []
    for video_id in batch:
        song = songs_by_video_id.get(video_id)
        if song is None:
            # No longer in the live liked list (e.g. unliked since the
            # snapshot was taken) -- nothing to classify here; U6's own
            # reconciliation (KTD10) handles staleness, not this pass.
            continue

        library_item = existing_by_video_id.get(video_id)
        if library_item is None:
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
                # A concurrent call for this user (a double-clicked "Finish
                # setup", or an overlapping retry after the UI looked stuck
                # on a slow batch -- each batch classifies up to
                # MATCHING_BATCH_SIZE songs one at a time, which can take
                # minutes) already created this LibraryItem between our
                # existing_by_video_id snapshot at the top of this call and
                # this insert. Roll back the failed insert and reuse the row
                # the other call created instead of crashing the whole batch
                # over one race (KTD18-style: one item's conflict shouldn't
                # block the rest).
                library_repository.session.rollback()
                library_item = library_repository.get_by_video_id(video_id)
                if library_item is None:
                    continue  # genuinely unexpected; skip this song this pass
            existing_by_video_id[video_id] = library_item

        batch_songs.append((song, library_item))

    # Phase 2 (parallel when `engine` is given): each song's classify_track
    # call is independent and dominated by a live LLM network round-trip.
    classify_results = classify_tracks_concurrently(
        [song for song, _ in batch_songs], candidates, classification_service, user_id, engine=engine
    )

    # Phase 3 (sequential DB writes): one review_queue_item per matched result.
    queue_items_created = 0
    processed = 0
    for (song, library_item), results in zip(batch_songs, classify_results):
        if results is None:
            # KTD18-style: this song's failure never blocks the rest of the
            # batch; it simply isn't marked processed, so a later batch
            # retries it.
            continue

        for result in results:
            pair = (library_item.id, result.playlist_id)
            if result.playlist_id is not None and pair in active_pairs:
                continue  # already an active/committed candidate there (KTD8)
            review_queue_repository.create(
                ReviewQueueItem(
                    user_id=user_id,
                    library_item_id=library_item.id,
                    playlist_id=result.playlist_id,
                    reorganize_session_id=reorganize_session_id,
                    confidence=result.confidence,
                    explanation=result.as_explanation_dict(),
                )
            )
            queue_items_created += 1
            if result.playlist_id is not None:
                active_pairs.add(pair)

        session_tagged_library_item_ids.add(library_item.id)
        processed += 1

    matching_complete = len(pending_video_ids) <= len(batch)

    return ReorganizeMatchingResult(
        ran=True,
        processed=processed,
        queue_items_created=queue_items_created,
        matching_complete=matching_complete,
    )


def run_reorganize_matching(
    reorganize_session_id: int, user_id: int, engine=None, music_client=None, classification_service=None
) -> None:
    """Background task (U4 follow-up, mirrors run_reorganize_clustering's
    pattern in library_analysis.py): triggered via FastAPI's
    `BackgroundTasks` after the trigger endpoint responds, so it opens its
    own DB session rather than reusing the (already-closed) request session.
    `engine`/`music_client` follow the same injectable-for-tests pattern.
    `classification_service` follows it too, and injecting it in tests isn't
    optional: leaving it unset builds a real one reading real API keys from Settings,
    which isn't guarded by mocking `completion`/`requests.get` at this call
    site, so an unset `classification_service` in a test can make a real,
    billed API call whenever real keys happen to be configured.

    Loops `run_reorganize_matching_batch` to completion, persisting
    `matched_count` progress after each batch so the poll endpoint has
    something durable to read. Replaces the old frontend-driven "call one
    batch, wait, call again" loop -- discovered the hard way that a single
    batch (up to MATCHING_BATCH_SIZE songs, each needing a live
    classification call) can take minutes, and a page navigation or closed
    tab used to abandon the loop mid-run with no way to resume it. Also
    means the frontend no longer needs to be the thing driving retries,
    which is what let two overlapping batches race to create the same
    LibraryItem in the first place (see reorganize_matching_batch's
    IntegrityError handling).
    """
    engine = engine or get_engine()
    music_client = music_client or YouTubeDataApiClient()
    with Session(engine) as db_session:
        reorganize_session_repo = ReorganizeSessionRepository(db_session)
        library_repo = LibraryRepository(db_session)
        playlist_repo = PlaylistRepository(db_session)
        review_queue_repo = ReviewQueueRepository(db_session)
        # Per-song classification only runs concurrently (see
        # classify_tracks_concurrently) when this function built
        # classification_service itself -- guaranteed real, so a worker
        # thread can safely reconstruct an independent copy of it bound to
        # its own DB session. A caller-supplied one (every test, per this
        # docstring's own "injecting it in tests isn't optional") might be a
        # mock; reconstructing from a mock's attributes would either raise
        # or silently ignore the mock's configured behavior, so
        # parallel_engine stays None in that case and
        # run_reorganize_matching_batch falls back to its original
        # single-threaded loop.
        parallel_engine = None
        if classification_service is None:
            genre_lookup = GenreLookupService(db_session)
            classification_service = ClassificationService(
                genre_lookup, correction_log_repo=CorrectionLogRepository(db_session)
            )
            parallel_engine = engine

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is None:
            return
        reorganize_session.matched_count = 0
        reorganize_session_repo.update(reorganize_session)

        # Built once for the whole run rather than every batch -- each
        # candidate's real YouTube track list was previously refetched on
        # every single run_reorganize_matching_batch call (same cost
        # jobs/ingestion.py's completion loop just fixed). Slightly stale
        # within one run if a playlist's real content changes mid-run --
        # self-corrects on the next trigger.
        candidates = [
            build_candidate(music_client, classification_service, playlist_repo, p)
            for p in playlist_repo.list_for_user(user_id)
        ]

        while True:
            result = run_reorganize_matching_batch(
                music_client=music_client,
                classification_service=classification_service,
                library_repository=library_repo,
                playlist_repository=playlist_repo,
                review_queue_repository=review_queue_repo,
                reorganize_session_repository=reorganize_session_repo,
                user_id=user_id,
                reorganize_session_id=reorganize_session_id,
                engine=parallel_engine,
                candidates=candidates,
            )
            if not result.ran:
                # Session vanished (cancelled mid-run) or the liked-songs
                # fetch failed. R14/KTD2: a degraded youtube_detection health
                # signal with zero matched progress so far this run means a
                # genuine failure -- write "failed" so the poll endpoint can
                # surface it instead of leaving matching_status stuck at
                # "in_progress" forever. Any other case (session gone, or a
                # fetch failure after this run already made real progress)
                # leaves matching_status untouched so a future trigger can
                # pick it back up.
                reorganize_session = reorganize_session_repo.get(reorganize_session_id)
                if reorganize_session is None:
                    return
                if dependency_health_store.failed_with_no_progress(
                    "youtube_detection", reorganize_session.matched_count > 0
                ):
                    reorganize_session.matching_status = "failed"
                    reorganize_session_repo.update(reorganize_session)
                return

            reorganize_session = reorganize_session_repo.get(reorganize_session_id)
            if reorganize_session is None:
                return
            reorganize_session.matched_count += result.processed
            reorganize_session_repo.update(reorganize_session)

            if result.matching_complete:
                break

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is not None:
            reorganize_session.matching_status = "done"
            reorganize_session_repo.update(reorganize_session)
