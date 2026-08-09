"""Tests for Finish & Apply (U6)."""

from datetime import timedelta
from unittest.mock import MagicMock

from app.models.base import utcnow
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.reorganize_session import ReorganizeSession
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.reorganize_apply import (
    ApplyAlreadyInProgressError,
    run_finish_and_apply,
    trigger_apply,
)


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


def _music_client(liked_video_ids=()) -> MagicMock:
    client = MagicMock()
    client.get_liked_songs.return_value = [
        {"videoId": vid, "title": f"Song {vid}", "artists": [{"name": "Artist"}]}
        for vid in liked_video_ids
    ]
    client.create_playlist.return_value = "yt-created"
    return client


def test_applying_creates_missing_playlists_and_leaves_existing_ones_untouched(session):
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)

    existing_playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Existing", description=None, rule=None, youtube_playlist_id="yt-existing")
    )
    placeholder_playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="New", description="theme", rule=None, youtube_playlist_id=None)
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1", "v2"], clustering_status="done", apply_status="in_progress"
        )
    )

    item1 = library_repo.create(LibraryItem(user_id=user.id, video_id="v1", title="A", artist="Artist"))
    item2 = library_repo.create(LibraryItem(user_id=user.id, video_id="v2", title="B", artist="Artist"))
    queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item1.id,
            playlist_id=existing_playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )
    queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item2.id,
            playlist_id=placeholder_playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )

    music_client = _music_client(["v1", "v2"])
    run_finish_and_apply(reorganize_session.id, user.id, music_client, engine=session.get_bind())

    refreshed_placeholder = playlist_repo.get(placeholder_playlist.id)
    assert refreshed_placeholder.youtube_playlist_id == "yt-created"
    music_client.create_playlist.assert_called_once_with("New", "theme")

    refreshed_existing = playlist_repo.get(existing_playlist.id)
    assert refreshed_existing.youtube_playlist_id == "yt-existing"

    all_items = queue_repo.list_for_user(user.id)
    assert all(item.status == "approved" for item in all_items)

    refreshed_session = reorganize_repo.get(reorganize_session.id)
    assert refreshed_session.apply_status == "idle"
    assert refreshed_session.apply_last_result["succeeded"] == 2
    assert refreshed_session.apply_last_result["failed"] == 0


def test_one_items_write_failure_does_not_block_the_rest_of_the_batch(session):
    """Covers AE3."""
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1", "v2"], clustering_status="done", apply_status="in_progress"
        )
    )
    item1 = library_repo.create(LibraryItem(user_id=user.id, video_id="v1", title="A", artist="Artist"))
    item2 = library_repo.create(LibraryItem(user_id=user.id, video_id="v2", title="B", artist="Artist"))
    queue_item1 = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item1.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )
    queue_item2 = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item2.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )

    music_client = _music_client(["v1", "v2"])

    def _side_effect(youtube_playlist_id, video_ids):
        if video_ids == ["v1"]:
            raise RuntimeError("transient API error")
        return None

    music_client.add_playlist_items.side_effect = _side_effect

    run_finish_and_apply(reorganize_session.id, user.id, music_client, engine=session.get_bind())

    refreshed1 = queue_repo.get(queue_item1.id)
    refreshed2 = queue_repo.get(queue_item2.id)
    assert refreshed1.status == "approved_pending_apply"  # failed, reverted, retryable
    assert refreshed2.status == "approved"  # the other item still succeeded

    refreshed_session = reorganize_repo.get(reorganize_session.id)
    assert refreshed_session.apply_last_result["succeeded"] == 1
    assert refreshed_session.apply_last_result["failed"] == 1
    assert refreshed_session.apply_last_result["failed_item_ids"] == [queue_item1.id]


def test_retriggering_apply_after_a_partial_run_only_processes_remaining_items(session):
    """Covers AE2."""
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1", "v2"], clustering_status="done", apply_status="in_progress"
        )
    )
    item1 = library_repo.create(LibraryItem(user_id=user.id, video_id="v1", title="A", artist="Artist"))
    item2 = library_repo.create(LibraryItem(user_id=user.id, video_id="v2", title="B", artist="Artist"))
    queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item1.id,
            playlist_id=playlist.id,
            status="approved",  # already applied in a previous run
            reorganize_session_id=reorganize_session.id,
        )
    )
    queue_item2 = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item2.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",  # still pending from the previous partial run
            reorganize_session_id=reorganize_session.id,
        )
    )

    music_client = _music_client(["v1", "v2"])
    run_finish_and_apply(reorganize_session.id, user.id, music_client, engine=session.get_bind())

    # Only the remaining item's write happened -- the already-applied one
    # wasn't re-written.
    music_client.add_playlist_items.assert_called_once_with("yt-rock", ["v2"])
    refreshed2 = queue_repo.get(queue_item2.id)
    assert refreshed2.status == "approved"


def test_write_pending_row_older_than_threshold_is_reverted_when_a_run_starts(session):
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1"], clustering_status="done", apply_status="in_progress"
        )
    )
    item = library_repo.create(LibraryItem(user_id=user.id, video_id="v1", title="A", artist="Artist"))
    stuck_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item.id,
            playlist_id=playlist.id,
            status="write_pending",
            reorganize_session_id=reorganize_session.id,
        )
    )
    stuck_item.updated_at = utcnow() - timedelta(minutes=10)
    session.add(stuck_item)
    session.commit()

    music_client = _music_client(["v1"])
    run_finish_and_apply(reorganize_session.id, user.id, music_client, engine=session.get_bind())

    refreshed = queue_repo.get(stuck_item.id)
    # Reverted to approved_pending_apply, then this same run picked it back
    # up and (since the write now succeeds) applied it.
    assert refreshed.status == "approved"


def test_song_unliked_mid_session_is_marked_stale_and_not_written(session):
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None, youtube_playlist_id="yt-rock")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1"], clustering_status="done", apply_status="in_progress"
        )
    )
    item = library_repo.create(LibraryItem(user_id=user.id, video_id="v1", title="A", artist="Artist"))
    queue_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )

    # No longer liked -- get_liked_songs returns nothing for v1.
    music_client = _music_client([])
    run_finish_and_apply(reorganize_session.id, user.id, music_client, engine=session.get_bind())

    refreshed = queue_repo.get(queue_item.id)
    assert refreshed.status == "stale"
    music_client.add_playlist_items.assert_not_called()


def test_triggering_apply_while_already_in_progress_is_rejected(session):
    user = _make_user(session)
    _, _, _, reorganize_repo = _repos(session)
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1"], clustering_status="done", apply_status="in_progress"
        )
    )

    try:
        trigger_apply(reorganize_repo, reorganize_session.id, user.id)
        assert False, "expected ApplyAlreadyInProgressError"
    except ApplyAlreadyInProgressError:
        pass


def test_a_playlist_creation_failure_does_not_block_other_playlists_items(session):
    user = _make_user(session)
    library_repo, playlist_repo, queue_repo, reorganize_repo = _repos(session)
    failing_playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Broken", description=None, rule=None, youtube_playlist_id=None)
    )
    ok_playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="OK", description=None, rule=None, youtube_playlist_id="yt-ok")
    )
    reorganize_session = reorganize_repo.create(
        ReorganizeSession(
            user_id=user.id, video_id_snapshot=["v1", "v2"], clustering_status="done", apply_status="in_progress"
        )
    )
    item1 = library_repo.create(LibraryItem(user_id=user.id, video_id="v1", title="A", artist="Artist"))
    item2 = library_repo.create(LibraryItem(user_id=user.id, video_id="v2", title="B", artist="Artist"))
    broken_queue_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item1.id,
            playlist_id=failing_playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )
    ok_queue_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=item2.id,
            playlist_id=ok_playlist.id,
            status="approved_pending_apply",
            reorganize_session_id=reorganize_session.id,
        )
    )

    music_client = _music_client(["v1", "v2"])
    music_client.create_playlist.side_effect = RuntimeError("playlist create failed")

    run_finish_and_apply(reorganize_session.id, user.id, music_client, engine=session.get_bind())

    refreshed_broken = queue_repo.get(broken_queue_item.id)
    refreshed_ok = queue_repo.get(ok_queue_item.id)
    assert refreshed_broken.status == "approved_pending_apply"  # stayed pending for retry
    assert refreshed_ok.status == "approved"  # unaffected by the other playlist's failure
