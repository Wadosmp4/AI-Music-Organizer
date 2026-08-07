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

from app.integrations.base import MusicServiceClient
from app.integrations.youtube_data_api_client import track_artist
from app.models.library import LibraryItem
from app.models.review_queue import ReviewQueueItem
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.classification import ClassificationService, build_candidate

# Same shape as jobs/ingestion.py's BACKFILL_BATCH_SIZE (KTD8) -- bounded so a
# single request can't burst GetSongBPM's rate ceiling.
MATCHING_BATCH_SIZE = 50


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
) -> ReorganizeMatchingResult:
    reorganize_session = reorganize_session_repository.get(reorganize_session_id)
    if reorganize_session is None or reorganize_session.user_id != user_id:
        return ReorganizeMatchingResult(
            ran=False, processed=0, queue_items_created=0, matching_complete=False
        )

    try:
        liked_songs = music_client.get_liked_songs()
    except Exception:
        return ReorganizeMatchingResult(
            ran=False, processed=0, queue_items_created=0, matching_complete=False
        )
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

    candidates = [
        build_candidate(music_client, classification_service, playlist_repository, p)
        for p in playlist_repository.list_for_user(user_id)
    ]

    queue_items_created = 0
    processed = 0
    for video_id in batch:
        song = songs_by_video_id.get(video_id)
        if song is None:
            # No longer in the live liked list (e.g. unliked since the
            # snapshot was taken) -- nothing to classify here; U6's own
            # reconciliation (KTD10) handles staleness, not this pass.
            continue

        library_item = existing_by_video_id.get(video_id)
        if library_item is None:
            library_item = library_repository.create(
                LibraryItem(
                    user_id=user_id,
                    video_id=song["videoId"],
                    title=song.get("title", ""),
                    artist=track_artist(song),
                )
            )
            existing_by_video_id[video_id] = library_item

        try:
            results = classification_service.classify_track(song, candidates, user_id=user_id)
        except Exception:
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
