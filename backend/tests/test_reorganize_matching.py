"""Tests for session-scoped full-library matching (U4)."""

from unittest.mock import MagicMock

from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.reorganize_session import ReorganizeSession
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.jobs.reorganize_matching import MATCHING_BATCH_SIZE, run_reorganize_matching_batch
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.classification import ClassificationResult, ClassificationService, Explanation


def _make_user(session) -> User:
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _repos(session):
    return (
        LibraryRepository(session),
        PlaylistRepository(session),
        ReviewQueueRepository(session),
        ReorganizeSessionRepository(session),
    )


def _music_client(liked_songs) -> MagicMock:
    client = MagicMock()
    client.get_liked_songs.return_value = liked_songs
    client.get_playlist_tracks.return_value = []
    return client


def _classification_service(results_by_video_id: dict) -> MagicMock:
    """results_by_video_id maps videoId -> list[ClassificationResult]. Falls
    back to a single no-match placeholder for any song not listed."""
    service = MagicMock(spec=ClassificationService)

    def _classify(track, candidates, user_id=None):
        return results_by_video_id.get(
            track["videoId"],
            [
                ClassificationResult(
                    playlist_id=None,
                    confidence=0.0,
                    explanation=Explanation("none", "no match"),
                    genre=None,
                )
            ],
        )

    service.classify_track.side_effect = _classify
    return service


def _song(video_id: str) -> dict:
    return {"videoId": video_id, "title": f"Song {video_id}", "artists": [{"name": "Artist"}]}


def test_already_committed_song_not_duplicated_but_matched_against_new_playlist(session):
    """A song already ingested & committed to playlist A via the ordinary
    lightweight loop isn't re-matched into a duplicate row for A, but IS
    matched against a brand-new playlist B introduced this session."""
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)

    playlist_a = playlist_repo.create(
        Playlist(user_id=user.id, name="A", description=None, rule=None, youtube_playlist_id="yt-a")
    )
    playlist_b = playlist_repo.create(
        Playlist(user_id=user.id, name="B", description=None, rule=None, youtube_playlist_id="yt-b")
    )

    library_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song v1", artist="Artist")
    )
    queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=library_item.id,
            playlist_id=playlist_a.id,
            status="approved",
        )
    )

    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["v1"], clustering_status="done")
    )

    classification_service = _classification_service(
        {
            "v1": [
                ClassificationResult(
                    playlist_id=playlist_a.id,
                    confidence=0.9,
                    explanation=Explanation("artist_similarity", "matched"),
                    genre=None,
                ),
                ClassificationResult(
                    playlist_id=playlist_b.id,
                    confidence=0.9,
                    explanation=Explanation("artist_similarity", "matched"),
                    genre=None,
                ),
            ]
        }
    )

    result = run_reorganize_matching_batch(
        music_client=_music_client([_song("v1")]),
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )

    assert result.ran is True
    assert result.queue_items_created == 1  # only the new B pairing, not a duplicate A row
    all_items = queue_repo.list_for_user(user.id)
    a_items = [i for i in all_items if i.playlist_id == playlist_a.id]
    b_items = [i for i in all_items if i.playlist_id == playlist_b.id]
    assert len(a_items) == 1  # still just the original, untouched
    assert len(b_items) == 1
    assert b_items[0].reorganize_session_id == reorganize_session.id


def test_genuinely_new_song_gets_a_library_item_and_one_item_per_matching_playlist(session):
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)

    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["new1"], clustering_status="done")
    )

    classification_service = _classification_service(
        {
            "new1": [
                ClassificationResult(
                    playlist_id=playlist.id,
                    confidence=0.9,
                    explanation=Explanation("artist_similarity", "matched"),
                    genre=None,
                )
            ]
        }
    )

    result = run_reorganize_matching_batch(
        music_client=_music_client([_song("new1")]),
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )

    assert result.queue_items_created == 1
    library_item = library_repo.get_by_video_id("new1")
    assert library_item is not None
    items = queue_repo.list_for_user(user.id)
    assert len(items) == 1
    assert items[0].library_item_id == library_item.id
    assert items[0].reorganize_session_id == reorganize_session.id


def test_concurrent_library_item_insert_is_recovered_not_crashed(session):
    """Reproduces a real production bug: two overlapping calls (a double-
    clicked "Finish setup", or a retry fired while a still-running slow
    batch -- up to MATCHING_BATCH_SIZE songs classified one at a time --
    hadn't returned yet) can both decide the same video_id needs a new
    LibraryItem and both try to create it; the second hits video_id's
    global UNIQUE constraint and used to crash the whole batch with a 500.
    Simulated by making create() insert the row for real (as the "other"
    caller would have) and then raise the same IntegrityError SQLite raises
    on a genuine concurrent insert."""
    from unittest.mock import patch

    from sqlalchemy.exc import IntegrityError

    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)

    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["racy1"], clustering_status="done")
    )
    classification_service = _classification_service(
        {
            "racy1": [
                ClassificationResult(
                    playlist_id=playlist.id,
                    confidence=0.9,
                    explanation=Explanation("artist_similarity", "matched"),
                    genre=None,
                )
            ]
        }
    )

    real_create = LibraryRepository.create

    def _racy_create(self, item):
        real_create(self, item)
        raise IntegrityError(
            "INSERT INTO library_item ...", {}, Exception("UNIQUE constraint failed: library_item.video_id")
        )

    with patch.object(LibraryRepository, "create", _racy_create):
        result = run_reorganize_matching_batch(
            music_client=_music_client([_song("racy1")]),
            classification_service=classification_service,
            library_repository=library_repo,
            playlist_repository=playlist_repo,
            review_queue_repository=queue_repo,
            reorganize_session_repository=reorganize_repo,
            user_id=user.id,
            reorganize_session_id=reorganize_session.id,
        )

    assert result.ran is True
    assert result.processed == 1
    library_item = library_repo.get_by_video_id("racy1")
    assert library_item is not None
    items = queue_repo.list_for_user(user.id)
    assert len(items) == 1
    assert items[0].library_item_id == library_item.id


def test_song_matching_nothing_still_gets_exactly_one_unassigned_row(session):
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["v1"], clustering_status="done")
    )

    result = run_reorganize_matching_batch(
        music_client=_music_client([_song("v1")]),
        classification_service=_classification_service({}),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )

    assert result.queue_items_created == 1
    items = queue_repo.list_for_user(user.id)
    assert len(items) == 1
    assert items[0].playlist_id is None
    assert items[0].reorganize_session_id == reorganize_session.id


def test_repeated_batches_process_snapshot_in_bounded_chunks(session):
    """Mirrors multiple 'Load next 50 songs' clicks -- same shape as today's
    backfill."""
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    video_ids = [f"v{i}" for i in range(MATCHING_BATCH_SIZE + 4)]
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=video_ids, clustering_status="done")
    )
    songs = [_song(vid) for vid in video_ids]

    first = run_reorganize_matching_batch(
        music_client=_music_client(songs),
        classification_service=_classification_service({}),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )
    assert first.processed == MATCHING_BATCH_SIZE
    assert first.matching_complete is False

    second = run_reorganize_matching_batch(
        music_client=_music_client(songs),
        classification_service=_classification_service({}),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )
    assert second.processed == 4
    assert second.matching_complete is True

    third = run_reorganize_matching_batch(
        music_client=_music_client(songs),
        classification_service=_classification_service({}),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )
    assert third.processed == 0
    assert third.matching_complete is True


def test_matching_ignores_songs_outside_the_session_snapshot(session):
    """Covers AE4's spirit at the matching layer: a re-triggered reorganize's
    (new or merged) snapshot is the sole source of truth for what this pass
    considers -- a liked song absent from it is left untouched here."""
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["v1"], clustering_status="done")
    )

    result = run_reorganize_matching_batch(
        music_client=_music_client([_song("v1"), _song("v2")]),
        classification_service=_classification_service({}),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        reorganize_session_repository=reorganize_repo,
        user_id=user.id,
        reorganize_session_id=reorganize_session.id,
    )

    assert result.processed == 1
    assert library_repo.get_by_video_id("v2") is None


def test_trigger_matching_marks_session_in_progress(session):
    from app.jobs.reorganize_matching import trigger_matching

    user = _make_user(session)
    _, _, _, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["v1"], clustering_status="done")
    )
    assert reorganize_session.matching_status == "idle"

    updated = trigger_matching(reorganize_repo, reorganize_session.id, user.id)

    assert updated.matching_status == "in_progress"
    assert reorganize_repo.get(reorganize_session.id).matching_status == "in_progress"


def test_trigger_matching_rejects_when_already_in_progress(session):
    from app.jobs.reorganize_matching import MatchingAlreadyInProgressError, trigger_matching

    user = _make_user(session)
    _, _, _, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1"], clustering_status="done", matching_status="in_progress"
        )
    )

    try:
        trigger_matching(reorganize_repo, reorganize_session.id, user.id)
        assert False, "expected MatchingAlreadyInProgressError"
    except MatchingAlreadyInProgressError:
        pass


def test_trigger_matching_raises_for_unknown_session(session):
    from app.jobs.reorganize_matching import trigger_matching
    from app.services.reorganize_apply import ReorganizeSessionNotFoundError

    _, _, _, reorganize_repo = _repos(session)

    try:
        trigger_matching(reorganize_repo, 999, 1)
        assert False, "expected ReorganizeSessionNotFoundError"
    except ReorganizeSessionNotFoundError:
        pass


def test_run_reorganize_matching_loops_to_completion_and_marks_done(session):
    """The background-task version (mirrors run_reorganize_clustering):
    triggered once, it runs every batch to completion itself instead of
    depending on the frontend to call it repeatedly, persisting matched_count
    progress along the way."""
    from app.jobs.reorganize_matching import run_reorganize_matching

    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    video_ids = [f"v{i}" for i in range(MATCHING_BATCH_SIZE + 4)]
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=video_ids, clustering_status="done")
    )

    classification_service = _classification_service(
        {
            vid: [
                ClassificationResult(
                    playlist_id=playlist.id,
                    confidence=0.9,
                    explanation=Explanation("artist_similarity", "matched"),
                    genre=None,
                )
            ]
            for vid in video_ids
        }
    )
    music_client = _music_client([_song(vid) for vid in video_ids])

    run_reorganize_matching(
        reorganize_session.id,
        user.id,
        engine=session.get_bind(),
        music_client=music_client,
        classification_service=classification_service,
    )

    final = reorganize_repo.get(reorganize_session.id)
    assert final.matching_status == "done"
    assert final.matched_count == len(video_ids)
    assert len(queue_repo.list_for_user(user.id)) == len(video_ids)


def test_run_reorganize_matching_stops_without_marking_done_if_session_is_cancelled_mid_run(session):
    """A session cancelled while matching is still running (e.g. the user
    hits "Cancel this session") must not have the background task overwrite
    matching_status back to "done" afterward."""
    from unittest.mock import patch

    from app.jobs.reorganize_matching import run_reorganize_matching

    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["v1"], clustering_status="done")
    )
    classification_service = _classification_service({})
    music_client = _music_client([_song("v1")])

    real_get = type(reorganize_repo).get

    def _cancel_then_get(self, session_id):
        result = real_get(self, session_id)
        if result is not None:
            result.clustering_status = "cancelled"
            self.session.add(result)
            self.session.delete(result)
            self.session.commit()
            return None
        return result

    with patch.object(type(reorganize_repo), "get", _cancel_then_get):
        run_reorganize_matching(
            reorganize_session.id,
            user.id,
            engine=session.get_bind(),
            music_client=music_client,
            classification_service=classification_service,
        )

    # The row was deleted by the patched get() above -- confirms
    # run_reorganize_matching returned early instead of trying to write a
    # "done" status back onto a session it can no longer find.
    assert reorganize_repo.get(reorganize_session.id) is None


def test_run_reorganize_matching_marks_failed_when_liked_songs_fetch_always_fails(session):
    """R14/KTD2: a background run that never makes any real progress
    because get_liked_songs() fails on every call must end matching_status
    == "failed" instead of leaving it stuck at "in_progress" forever, which
    is what happened before this fix -- the poll endpoint had no way to
    distinguish a genuinely broken run from a legitimately still-running
    one."""
    from app.jobs.reorganize_matching import run_reorganize_matching

    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=["v1"], clustering_status="done")
    )
    classification_service = _classification_service({})
    music_client = MagicMock()
    music_client.get_liked_songs.side_effect = RuntimeError("network error")

    run_reorganize_matching(
        reorganize_session.id,
        user.id,
        engine=session.get_bind(),
        music_client=music_client,
        classification_service=classification_service,
    )

    final = reorganize_repo.get(reorganize_session.id)
    assert final.matching_status == "failed"
    assert final.matched_count == 0

    dependency_health_store.set_status("youtube_detection", DependencyStatus.OK)


def test_run_reorganize_matching_marks_done_when_fetch_is_healthy_but_snapshot_already_processed(session):
    """Regression guard: a run with a perfectly healthy fetch whose full
    snapshot was already processed by a prior pass (so this run legitimately
    has nothing new to match) must still end matching_status == "done", not
    "failed"."""
    from app.jobs.reorganize_matching import run_reorganize_matching

    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    dependency_health_store.set_status("youtube_detection", DependencyStatus.OK)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(user_id=user.id, video_id_snapshot=[], clustering_status="done")
    )
    classification_service = _classification_service({})
    music_client = _music_client([])

    run_reorganize_matching(
        reorganize_session.id,
        user.id,
        engine=session.get_bind(),
        music_client=music_client,
        classification_service=classification_service,
    )

    final = reorganize_repo.get(reorganize_session.id)
    assert final.matching_status == "done"
    assert final.matched_count == 0
    status, _ = dependency_health_store.get_status("youtube_detection")
    assert status == DependencyStatus.OK
