from unittest.mock import MagicMock

from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.jobs.ingestion import BACKFILL_BATCH_SIZE, reset_backlog, run_ingestion_check
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.services.classification import ClassificationResult, ClassificationService, Explanation


def _onboarded_user(session) -> User:
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return UserRepository(session).mark_onboarding_completed(user.id)


def _repos(session):
    return (
        LibraryRepository(session),
        PlaylistRepository(session),
        ReviewQueueRepository(session),
        UserRepository(session),
    )


def _matching_classification_service(playlist_id: int) -> MagicMock:
    service = MagicMock(spec=ClassificationService)
    service.classify_track.return_value = ClassificationResult(
        playlist_id=playlist_id,
        confidence=0.8,
        explanation=Explanation("artist_similarity", "matched"),
        bpm=None,
        bpm_source=None,
        genre=None,
    )
    return service


def _no_match_classification_service() -> MagicMock:
    service = MagicMock(spec=ClassificationService)
    service.classify_track.return_value = ClassificationResult(
        playlist_id=None,
        confidence=0.0,
        explanation=Explanation("none", "no match"),
        bpm=None,
        bpm_source=None,
        genre=None,
    )
    return service


def test_check_is_gated_until_onboarding_completes(session):
    user = User(display_name="Not Onboarded")
    session.add(user)
    session.commit()
    session.refresh(user)

    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v1", "title": "Song", "artists": [{"name": "Artist"}]}
    ]

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=_no_match_classification_service(),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.ran is False
    assert result.mode == "waiting_for_onboarding"
    music_client.get_liked_songs.assert_not_called()
    assert library_repo.list_for_user(user.id) == []


def test_new_liked_song_produces_exactly_one_queue_item(session):
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v1", "title": "Song A", "artists": [{"name": "Artist"}]}
    ]

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=_matching_classification_service(playlist.id),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.new_songs_found == 1
    assert result.queue_items_created == 1
    items = queue_repo.list_for_user(user.id)
    assert len(items) == 1
    assert items[0].playlist_id == playlist.id


def test_unmatched_new_song_still_produces_a_queue_item_so_it_can_be_manually_assigned(session):
    """Previously an unmatched song (playlist_id=None) never got a
    review_queue_item at all -- the LibraryItem existed but was invisible
    everywhere and could never be manually assigned to a playlist. It now
    gets a queue item like any other song, just with playlist_id=None,
    surfacing in the Review Queue's "Unassigned" group."""
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v1", "title": "Song A", "artists": [{"name": "Artist"}]}
    ]

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=_no_match_classification_service(),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.queue_items_created == 1
    items = queue_repo.list_for_user(user.id)
    assert len(items) == 1
    assert items[0].playlist_id is None


def test_already_placed_song_produces_none(session):
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v1", "title": "Song A", "artists": [{"name": "Artist"}]}
    ]

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=_no_match_classification_service(),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.new_songs_found == 0
    assert result.queue_items_created == 0
    assert len(library_repo.list_for_user(user.id)) == 1  # not duplicated


def test_calling_check_again_does_not_reprocess_already_seen_songs(session):
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v1", "title": "Song A", "artists": [{"name": "Artist"}]}
    ]
    classification_service = _matching_classification_service(playlist.id)

    first = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )
    second = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert first.new_songs_found == 1
    assert second.new_songs_found == 0
    assert len(library_repo.list_for_user(user.id)) == 1
    assert len(queue_repo.list_for_user(user.id)) == 1


def test_backfill_processes_backlog_in_bounded_batches_and_resumes(session):
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    # More songs than one batch can hold.
    songs = [
        {"videoId": f"v{i}", "title": f"Song {i}", "artists": [{"name": "Artist"}]}
        for i in range(120)
    ]
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = songs
    classification_service = _matching_classification_service(playlist.id)

    first = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )
    assert first.mode == "backfill"
    assert first.new_songs_found == 50  # BACKFILL_BATCH_SIZE
    assert first.backfill_complete is False
    fresh_user = user_repo.get(user.id)
    assert fresh_user.backfill_completed_at is None

    second = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )
    assert second.new_songs_found == 50
    assert second.backfill_complete is False

    third = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )
    assert third.new_songs_found == 20  # remaining 120 - 50 - 50
    assert third.backfill_complete is True
    fresh_user = user_repo.get(user.id)
    assert fresh_user.backfill_completed_at is not None

    assert len(library_repo.list_for_user(user.id)) == 120
    assert len(queue_repo.list_for_user(user.id)) == 120

    # Steady state now: a brand-new song shows up, still processed normally.
    songs.append({"videoId": "v-new", "title": "New Song", "artists": [{"name": "Artist"}]})
    steady_state = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )
    assert steady_state.mode == "steady_state"
    assert steady_state.new_songs_found == 1


def test_liked_songs_fetch_failure_surfaces_degraded_health_without_raising(session):
    """The other of #8's two previously-untested exception branches: a
    get_liked_songs() failure must degrade youtube_detection health and
    return a ran=True/zero-progress result, not raise past run_ingestion_check."""
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    music_client = MagicMock()
    music_client.get_liked_songs.side_effect = RuntimeError("network error")

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=_no_match_classification_service(),
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.ran is True
    assert result.new_songs_found == 0
    assert result.queue_items_created == 0
    status, reason = dependency_health_store.get_status("youtube_detection")
    assert status == DependencyStatus.DEGRADED
    assert "network error" in reason.lower()

    dependency_health_store.set_status("youtube_detection", DependencyStatus.OK)


def test_steady_state_check_is_bounded_the_same_as_backfill(session):
    """#5's fix: previously `limit = BACKFILL_BATCH_SIZE if is_backfill else
    None` left steady-state completely unbounded — a user returning after
    time away and liking a large burst of songs would process all of them
    synchronously in one call, defeating the rate-limit protection the cap
    exists for (KTD21)."""
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    user_repo.mark_backfill_completed(user.id)  # already in steady_state
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    songs = [
        {"videoId": f"burst-{i}", "title": f"Song {i}", "artists": [{"name": "Artist"}]}
        for i in range(BACKFILL_BATCH_SIZE + 30)
    ]
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = songs
    classification_service = _matching_classification_service(playlist.id)

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.mode == "steady_state"
    assert result.new_songs_found == BACKFILL_BATCH_SIZE  # capped, not all 80
    assert len(library_repo.list_for_user(user.id)) == BACKFILL_BATCH_SIZE

    # The remaining songs are picked up on the next call, same as backfill.
    second = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )
    assert second.new_songs_found == 30


def test_classification_failure_leaves_song_out_of_membership_index_so_the_next_check_retries_it(
    session,
):
    """#13's fix: the LibraryItem row used to be created BEFORE classify_track
    ran, so a classification failure left the song "already known"
    (existing_video_ids) on the next check despite the code's own comment
    claiming it would be retried — it was actually orphaned forever. Now the
    LibraryItem is only created once classification has actually run without
    raising, so a genuinely transient failure keeps the song eligible for
    the next check's new_songs list.
    """
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v-flaky", "title": "Flaky Song", "artists": [{"name": "Artist"}]}
    ]
    classification_service = MagicMock(spec=ClassificationService)
    classification_service.classify_track.side_effect = RuntimeError("transient failure")

    first = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert first.new_songs_found == 1
    assert first.queue_items_created == 0
    assert library_repo.list_for_user(user.id) == []  # not created on failure

    # A real retry: classification now succeeds for the same song.
    classification_service.classify_track.side_effect = None
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    classification_service.classify_track.return_value = ClassificationResult(
        playlist_id=playlist.id,
        confidence=0.8,
        explanation=Explanation("artist_similarity", "matched"),
        bpm=None,
        bpm_source=None,
        genre=None,
    )

    second = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert second.new_songs_found == 1  # genuinely retried, not silently orphaned
    assert second.queue_items_created == 1
    assert len(library_repo.list_for_user(user.id)) == 1


def test_ingestion_staleness_write_racing_concurrent_user_action_is_a_conflict_not_overwrite(session, engine):
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    library_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_item = queue_repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=library_item.id, playlist_id=playlist.id)
    )

    # The ingestion check runs in its own DB session/request — a genuinely
    # separate Session (not just a separate variable) is required here: two
    # ReviewQueueRepository instances sharing one Session would return the
    # SAME identity-mapped ORM object for this row, so a "concurrent" write
    # through one would silently mutate the other's in-memory snapshot too,
    # masking the exact race this test exists to catch.
    from sqlmodel import Session as SQLSession

    from app.jobs.ingestion import _mark_removed_songs

    ingestion_session = SQLSession(engine)
    ingestion_queue_repo = ReviewQueueRepository(ingestion_session)
    ingestion_library_repo = LibraryRepository(ingestion_session)

    queue_items_snapshot = ingestion_queue_repo.list_for_user(user.id)  # version 1

    # Concurrent user action, in the original `session` — approve moves this
    # row to write_pending and bumps its version, committed independently of
    # the ingestion session's already-taken snapshot.
    queue_repo.update(queue_item.id, expected_version=1, status="write_pending")

    # The song is no longer in the liked list — the ingestion session tries to
    # mark its queue item stale using the pre-race snapshot's version (1),
    # which is now stale against the real row (version 2).
    songs_marked_removed = _mark_removed_songs(
        existing_items=[ingestion_library_repo.get(library_item.id)],
        liked_video_ids=set(),
        queue_items_snapshot=queue_items_snapshot,
        library_repository=ingestion_library_repo,
        review_queue_repository=ingestion_queue_repo,
    )
    ingestion_session.close()

    assert songs_marked_removed == 1  # the library item itself is still marked removed
    refreshed_queue_item = queue_repo.get(queue_item.id)
    # The CAS conflict means the status the concurrent action set is preserved,
    # never silently overwritten back to "stale" (KTD19).
    assert refreshed_queue_item.status == "write_pending"
    assert refreshed_queue_item.version == 2


def test_reset_backlog_clears_uncommitted_songs_and_restarts_backfill(session):
    user = _onboarded_user(session)
    UserRepository(session).mark_backfill_completed(user.id)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)

    library_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=library_item.id, playlist_id=None)
    )

    result = reset_backlog(
        library_repository=library_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.library_items_cleared == 1
    assert library_repo.list_for_user(user.id) == []
    assert queue_repo.list_for_user(user.id) == []
    fresh_user = user_repo.get(user.id)
    assert fresh_user.backfill_completed_at is None


def test_reset_backlog_leaves_approved_and_moved_songs_untouched(session):
    user = _onboarded_user(session)
    UserRepository(session).mark_backfill_completed(user.id)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )

    approved_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v-approved", title="Approved Song", artist="Artist")
    )
    approved_queue_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=approved_item.id,
            playlist_id=playlist.id,
            status="approved",
        )
    )
    pending_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v-pending", title="Pending Song", artist="Artist")
    )
    queue_repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=pending_item.id, playlist_id=playlist.id)
    )

    result = reset_backlog(
        library_repository=library_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.library_items_cleared == 1
    remaining_items = library_repo.list_for_user(user.id)
    assert [item.id for item in remaining_items] == [approved_item.id]
    remaining_queue_items = queue_repo.list_for_user(user.id)
    assert [item.id for item in remaining_queue_items] == [approved_queue_item.id]
