"""Tests for session-scoped full-library matching (U4)."""

from unittest.mock import MagicMock

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
                    bpm=None,
                    bpm_source=None,
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
                    bpm=None,
                    bpm_source=None,
                    genre=None,
                ),
                ClassificationResult(
                    playlist_id=playlist_b.id,
                    confidence=0.9,
                    explanation=Explanation("artist_similarity", "matched"),
                    bpm=None,
                    bpm_source=None,
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
                    bpm=None,
                    bpm_source=None,
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
