import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session as SQLSession

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.reorganize_session import ReorganizeSession
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError
from app.services.review_queue import ItemNotFoundError, ReviewQueueService, StaleItemError


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


def test_approve_against_playlist_missing_youtube_id_raises_without_misreporting_auth(
    session, seeded
):
    """A playlist created without a linked YouTube playlist (the #1 bug this
    review fixed) must fail loudly and distinctly from a real write-path
    failure: no auth_status flip, and the item must stay 'pending' rather
    than getting stuck in 'write_pending'.
    """
    unlinked_playlist = Playlist(user_id=seeded["user"].id, name="Unlinked", youtube_playlist_id=None)
    session.add(unlinked_playlist)
    session.commit()
    session.refresh(unlinked_playlist)
    seeded["queue_item"].playlist_id = unlinked_playlist.id
    session.add(seeded["queue_item"])
    session.commit()

    music_client = MagicMock()
    service = _service(session, music_client)

    with pytest.raises(ItemNotFoundError):
        service.approve(seeded["queue_item"].id, expected_version=1)

    music_client.add_playlist_items.assert_not_called()
    status, _ = auth_status_store.get_write_status()
    assert status == AuthStatus.OK  # not misreported as a write-path auth failure
    refreshed = service.review_queue_repo.get(seeded["queue_item"].id)
    assert refreshed.status == "pending"  # never entered write_pending


def test_move_against_destination_missing_youtube_id_raises_without_misreporting_auth(
    session, seeded
):
    unlinked_playlist = Playlist(user_id=seeded["user"].id, name="Unlinked", youtube_playlist_id=None)
    session.add(unlinked_playlist)
    session.commit()
    session.refresh(unlinked_playlist)

    music_client = MagicMock()
    service = _service(session, music_client)

    with pytest.raises(ItemNotFoundError):
        service.move(seeded["queue_item"].id, expected_version=1, new_playlist_id=unlinked_playlist.id)

    music_client.add_playlist_items.assert_not_called()
    status, _ = auth_status_store.get_write_status()
    assert status == AuthStatus.OK
    refreshed = service.review_queue_repo.get(seeded["queue_item"].id)
    assert refreshed.status == "pending"
    assert refreshed.playlist_id == seeded["playlist"].id  # never reassigned


def test_ingestion_staleness_race_after_write_pending_is_diagnosable_not_silently_masked(
    session, engine, seeded, caplog
):
    """Inverse of the ordering covered in test_ingestion.py: here the
    ingestion staleness-CAS runs and WINS after write_pending was already
    set (e.g. the song was unliked in the same window an approve was in
    flight). The completion CAS (write_pending -> approved) then loses its
    own race and raises VersionConflictError even though the write to
    YouTube Music genuinely succeeded (#7's finding). The 409 contract must
    stay unchanged, but the outcome must now be diagnosable via a log line
    instead of silently indistinguishable from an ordinary conflict.
    """
    # A genuinely separate Session sharing the same engine — see
    # test_ingestion.py's identity-map note for why this must not share
    # `session` (a shared Session would silently mutate both repos' view of
    # the row, masking the exact race this test exists to catch).
    concurrent_session = SQLSession(engine)
    concurrent_queue_repo = ReviewQueueRepository(concurrent_session)

    def _concurrent_ingestion_marks_stale(*args, **kwargs):
        concurrent_queue_repo.update(seeded["queue_item"].id, expected_version=2, status="stale")

    music_client = MagicMock()
    music_client.add_playlist_items.side_effect = _concurrent_ingestion_marks_stale
    service = _service(session, music_client)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(VersionConflictError):
            service.approve(seeded["queue_item"].id, expected_version=1)

    music_client.add_playlist_items.assert_called_once()  # the write really did succeed
    concurrent_session.close()

    refreshed = ReviewQueueRepository(session).get(seeded["queue_item"].id)
    assert refreshed.status == "stale"  # ingestion's status is preserved, never silently overwritten
    assert any(
        "completion CAS" in record.message and "succeeded" in record.message
        for record in caplog.records
    )


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


def test_add_to_playlist_on_unassigned_item_reassigns_without_writing(session):
    """Never an eager write (unlike move): this is a pending first-placement
    decision, not a commit -- it still waits for that playlist's Approve-all."""
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    playlist = Playlist(user_id=user.id, name="Rock", youtube_playlist_id="PL123")
    session.add(playlist)
    session.commit()
    session.refresh(playlist)
    library_item = LibraryItem(user_id=user.id, video_id="vid1", title="Song", artist="Artist")
    session.add(library_item)
    session.commit()
    session.refresh(library_item)
    queue_item = ReviewQueueItem(user_id=user.id, library_item_id=library_item.id, playlist_id=None)
    session.add(queue_item)
    session.commit()
    session.refresh(queue_item)

    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.add_to_playlist(queue_item.id, expected_version=1, target_playlist_id=playlist.id)

    assert result.id == queue_item.id
    assert result.status == "pending"
    assert result.playlist_id == playlist.id
    music_client.add_playlist_items.assert_not_called()


def test_add_to_playlist_on_an_already_assigned_item_creates_an_independent_pending_candidate(
    session, seeded
):
    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.add_to_playlist(
        seeded["queue_item"].id, expected_version=1, target_playlist_id=seeded["other_playlist"].id
    )

    # A brand new row -- the original suggestion is completely untouched, so
    # each placement is approved (and written) independently later.
    assert result.id != seeded["queue_item"].id
    assert result.playlist_id == seeded["other_playlist"].id
    assert result.status == "pending"
    original = service.review_queue_repo.get(seeded["queue_item"].id)
    assert original.playlist_id == seeded["playlist"].id
    assert original.status == "pending"
    music_client.add_playlist_items.assert_not_called()
    assert service.correction_log_repo.list_for_user(seeded["user"].id) == []


def test_add_to_playlist_does_not_duplicate_an_already_active_candidate(session, seeded):
    """Clicking "Add to X" twice (or X already holding this song's original
    suggestion) must not create two pending rows for the same song/playlist."""
    music_client = MagicMock()
    service = _service(session, music_client)

    first = service.add_to_playlist(
        seeded["queue_item"].id, expected_version=1, target_playlist_id=seeded["other_playlist"].id
    )
    second = service.add_to_playlist(
        seeded["queue_item"].id, expected_version=1, target_playlist_id=seeded["other_playlist"].id
    )

    assert second.id == first.id
    all_items = service.review_queue_repo.list_for_user(seeded["user"].id)
    assert len(all_items) == 2  # original + the one new candidate, not three


def test_add_to_playlist_targeting_the_items_current_playlist_is_a_noop(session, seeded):
    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.add_to_playlist(
        seeded["queue_item"].id, expected_version=1, target_playlist_id=seeded["playlist"].id
    )

    assert result.id == seeded["queue_item"].id
    music_client.add_playlist_items.assert_not_called()


# -- U5: deferred-write approve/move for session-tagged items ----------------


def test_approving_a_session_tagged_item_defers_the_write(session, seeded):
    reorganize_session = ReorganizeSession(user_id=seeded["user"].id, video_id_snapshot=["vid1"])
    session.add(reorganize_session)
    session.commit()
    session.refresh(reorganize_session)
    seeded["queue_item"].reorganize_session_id = reorganize_session.id
    session.add(seeded["queue_item"])
    session.commit()

    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.approve(seeded["queue_item"].id, expected_version=1)

    assert result.status == "approved_pending_apply"
    music_client.add_playlist_items.assert_not_called()


def test_approving_a_non_session_item_behaves_exactly_as_today(session, seeded):
    """AE1: no session tag -> immediate write, unchanged."""
    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.approve(seeded["queue_item"].id, expected_version=1)

    assert result.status == "approved"
    music_client.add_playlist_items.assert_called_once_with("PL123", ["vid1"])


def test_moving_a_session_tagged_item_defers_the_write_but_still_logs_a_correction(session, seeded):
    reorganize_session = ReorganizeSession(user_id=seeded["user"].id, video_id_snapshot=["vid1"])
    session.add(reorganize_session)
    session.commit()
    session.refresh(reorganize_session)
    seeded["queue_item"].reorganize_session_id = reorganize_session.id
    session.add(seeded["queue_item"])
    session.commit()

    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.move(
        seeded["queue_item"].id, expected_version=1, new_playlist_id=seeded["other_playlist"].id
    )

    assert result.status == "approved_pending_apply"
    assert result.playlist_id == seeded["other_playlist"].id
    music_client.add_playlist_items.assert_not_called()

    [correction] = service.correction_log_repo.list_for_user(seeded["user"].id)
    assert correction.original_playlist_id == seeded["playlist"].id
    assert correction.corrected_playlist_id == seeded["other_playlist"].id


def test_adding_a_session_tagged_item_to_a_second_playlist_inherits_the_session_tag(session, seeded):
    reorganize_session = ReorganizeSession(user_id=seeded["user"].id, video_id_snapshot=["vid1"])
    session.add(reorganize_session)
    session.commit()
    session.refresh(reorganize_session)
    seeded["queue_item"].reorganize_session_id = reorganize_session.id
    session.add(seeded["queue_item"])
    session.commit()

    music_client = MagicMock()
    service = _service(session, music_client)

    result = service.add_to_playlist(
        seeded["queue_item"].id, expected_version=1, target_playlist_id=seeded["other_playlist"].id
    )

    assert result.id != seeded["queue_item"].id
    assert result.reorganize_session_id == reorganize_session.id
    music_client.add_playlist_items.assert_not_called()
