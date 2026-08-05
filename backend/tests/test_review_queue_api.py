from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError
from app.services.review_queue import ReviewQueueService, StaleItemError


@pytest.fixture(autouse=True)
def reset_write_status():
    auth_status_store.set_write_status(AuthStatus.OK)
    yield
    auth_status_store.set_write_status(AuthStatus.OK)


@pytest.fixture()
def seeded(session):
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)

    playlist = Playlist(user_id=user.id, name="Rock", youtube_playlist_id="PL123")
    other_playlist = Playlist(user_id=user.id, name="Chill", youtube_playlist_id="PL456")
    session.add(playlist)
    session.add(other_playlist)
    session.commit()
    session.refresh(playlist)
    session.refresh(other_playlist)

    library_item = LibraryItem(user_id=user.id, video_id="vid1", title="Song", artist="Artist")
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    queue_item = ReviewQueueItem(
        user_id=user.id, library_item_id=library_item.id, playlist_id=playlist.id
    )
    session.add(queue_item)
    session.commit()
    session.refresh(queue_item)

    return {
        "user": user,
        "playlist": playlist,
        "other_playlist": other_playlist,
        "library_item": library_item,
        "queue_item": queue_item,
    }


def _service(session, music_client=None):
    return ReviewQueueService(
        review_queue_repo=ReviewQueueRepository(session),
        library_repo=LibraryRepository(session),
        playlist_repo=PlaylistRepository(session),
        correction_log_repo=CorrectionLogRepository(session),
        music_client=music_client or MagicMock(),
    )


def test_approve_transitions_through_write_pending_and_applies_one_mutation(session, seeded):
    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.approve(seeded["queue_item"].id, expected_version=1)

    assert result.status == "approved"
    music_client.add_playlist_items.assert_called_once_with("PL123", ["vid1"])


def test_write_failure_during_approve_returns_to_pending_and_raises_health(session, seeded):
    music_client = MagicMock()
    music_client.add_playlist_items.side_effect = RuntimeError("network error")
    service = _service(session, music_client)

    with pytest.raises(RuntimeError):
        service.approve(seeded["queue_item"].id, expected_version=1)

    refreshed = service.review_queue_repo.get(seeded["queue_item"].id)
    assert refreshed.status == "pending"
    status, _ = auth_status_store.get_write_status()
    assert status == AuthStatus.NEEDS_RECONNECT


def test_reject_leaves_playlist_untouched_and_logs_no_correction(session, seeded):
    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.reject(seeded["queue_item"].id, expected_version=1)

    assert result.status == "rejected"
    music_client.add_playlist_items.assert_not_called()
    assert service.correction_log_repo.list_for_user(seeded["user"].id) == []


def test_move_logs_a_correction_with_old_and_new_destination(session, seeded):
    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.move(
        seeded["queue_item"].id, expected_version=1, new_playlist_id=seeded["other_playlist"].id
    )

    assert result.status == "moved"
    assert result.playlist_id == seeded["other_playlist"].id
    music_client.add_playlist_items.assert_called_once_with("PL456", ["vid1"])

    [correction] = service.correction_log_repo.list_for_user(seeded["user"].id)
    assert correction.original_playlist_id == seeded["playlist"].id
    assert correction.corrected_playlist_id == seeded["other_playlist"].id


def test_second_action_against_resolved_item_returns_conflict_not_double_apply(session, seeded):
    music_client = MagicMock()
    service = _service(session, music_client)

    service.approve(seeded["queue_item"].id, expected_version=1)

    with pytest.raises(VersionConflictError):
        service.approve(seeded["queue_item"].id, expected_version=1)

    music_client.add_playlist_items.assert_called_once()  # not called a second time


def test_high_confidence_suggestion_still_requires_explicit_approval(session, seeded):
    seeded["queue_item"].confidence = 0.99
    session.add(seeded["queue_item"])
    session.commit()

    fresh = ReviewQueueRepository(session).get(seeded["queue_item"].id)
    assert fresh.status == "pending"  # confidence alone never transitions status — AE3

    music_client = MagicMock()
    service = _service(session, music_client)
    result = service.approve(seeded["queue_item"].id, expected_version=1)
    assert result.status == "approved"
    music_client.add_playlist_items.assert_called_once()


def test_stale_item_surfaced_synchronously_rather_than_approvable(session, seeded):
    seeded["library_item"].removed_at = datetime.now(timezone.utc)
    session.add(seeded["library_item"])
    session.commit()

    music_client = MagicMock()
    service = _service(session, music_client)

    with pytest.raises(StaleItemError):
        service.approve(seeded["queue_item"].id, expected_version=1)

    music_client.add_playlist_items.assert_not_called()
    refreshed = service.review_queue_repo.get(seeded["queue_item"].id)
    assert refreshed.status == "stale"
