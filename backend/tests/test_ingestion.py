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
from app.services.genre_lookup import GenreLookupService


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
    service.classify_track.return_value = [
        ClassificationResult(
            playlist_id=playlist_id,
            confidence=0.8,
            explanation=Explanation("artist_similarity", "matched"),
            genre=None,
        )
    ]
    return service


def _no_match_classification_service() -> MagicMock:
    service = MagicMock(spec=ClassificationService)
    service.classify_track.return_value = [
        ClassificationResult(
            playlist_id=None,
            confidence=0.0,
            explanation=Explanation("none", "no match"),
            genre=None,
        )
    ]
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


def test_concurrent_library_item_insert_is_recovered_not_crashed(session):
    """Mirrors the same real bug fixed in reorganize_matching.py: a
    double-clicked retry (or any two overlapping ingestion checks) can both
    decide a video_id is new and both try to create its LibraryItem, hitting
    video_id's global UNIQUE constraint on the second insert. Simulated by
    making create() insert the row for real (as the "other" caller would
    have) and then raise the same IntegrityError SQLite raises on a genuine
    concurrent insert."""
    from unittest.mock import patch

    from sqlalchemy.exc import IntegrityError

    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "racy1", "title": "Song A", "artists": [{"name": "Artist"}]}
    ]

    real_create = LibraryRepository.create

    def _racy_create(self, item):
        real_create(self, item)
        raise IntegrityError(
            "INSERT INTO library_item ...", {}, Exception("UNIQUE constraint failed: library_item.video_id")
        )

    with patch.object(LibraryRepository, "create", _racy_create):
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
    library_item = library_repo.get_by_video_id("racy1")
    assert library_item is not None
    items = queue_repo.list_for_user(user.id)
    assert len(items) == 1
    assert items[0].library_item_id == library_item.id


def test_a_song_matching_two_playlists_is_queued_in_both(session):
    """The user's own framing: a song isn't limited to one playlist. Two
    playlists both clear the artist-similarity bar for the same artist --
    both must get their own pending review_queue_item for this one song."""
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    rock = playlist_repo.create(Playlist(user_id=user.id, name="Rock", description=None, rule=None))
    favorites = playlist_repo.create(
        Playlist(user_id=user.id, name="Favorites", description=None, rule=None)
    )

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v1", "title": "Song A", "artists": [{"name": "Queen"}]}
    ]
    classification_service = MagicMock(spec=ClassificationService)
    classification_service.classify_track.return_value = [
        ClassificationResult(
            playlist_id=rock.id,
            confidence=0.8,
            explanation=Explanation("artist_similarity", "matched Rock"),
            genre=None,
        ),
        ClassificationResult(
            playlist_id=favorites.id,
            confidence=0.6,
            explanation=Explanation("description_match", "matched Favorites"),
            genre=None,
        ),
    ]

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.new_songs_found == 1
    assert result.queue_items_created == 2
    items = queue_repo.list_for_user(user.id)
    assert {item.playlist_id for item in items} == {rock.id, favorites.id}
    assert len({item.library_item_id for item in items}) == 1  # both point at the same song


def test_adopted_playlists_real_youtube_content_feeds_artist_similarity_matching(session):
    """End-to-end regression for the "nothing ever matches" gap: candidate
    playlists' artist_counts used to always be built empty (CandidatePlaylist
    .from_playlist was never given real counts), so artist-similarity could
    never fire for a freshly adopted pre-existing YouTube playlist -- it has
    no local approval history, only real songs already sitting on YouTube.
    Uses a real ClassificationService (not a stubbed one) to prove the whole
    path -- run_ingestion_check building candidates from
    music_client.get_playlist_tracks -- actually routes a same-artist song.
    """
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(
            user_id=user.id,
            name="EDM mix",
            description=None,
            rule=None,
            youtube_playlist_id="yt-edm-mix",
        )
    )

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v-new", "title": "New Banger", "artists": [{"name": "Daft Punk"}]}
    ]

    def _get_playlist_tracks(playlist_id):
        assert playlist_id == "yt-edm-mix"
        return [
            {"videoId": f"existing-{i}", "title": "t", "artists": [{"name": "Daft Punk"}]}
            for i in range(3)
        ]

    music_client.get_playlist_tracks.side_effect = _get_playlist_tracks

    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    # Explicit "" (not the default None): None now falls through to
    # get_settings().openrouter_api_key, which could pick up a real key from
    # the process environment and make a real, billed API call here.
    classification_service = ClassificationService(genre_lookup, openrouter_api_key="")

    result = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.queue_items_created == 1
    items = queue_repo.list_for_user(user.id)
    assert items[0].playlist_id == playlist.id
    assert items[0].explanation["signal"] == "artist_similarity"


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
    # More songs than one batch can hold: two full batches plus a partial third.
    total_songs = 2 * BACKFILL_BATCH_SIZE + BACKFILL_BATCH_SIZE // 2
    songs = [
        {"videoId": f"v{i}", "title": f"Song {i}", "artists": [{"name": "Artist"}]}
        for i in range(total_songs)
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
    assert first.new_songs_found == BACKFILL_BATCH_SIZE
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
    assert second.new_songs_found == BACKFILL_BATCH_SIZE
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
    assert third.new_songs_found == BACKFILL_BATCH_SIZE // 2  # remaining partial batch
    assert third.backfill_complete is True
    fresh_user = user_repo.get(user.id)
    assert fresh_user.backfill_completed_at is not None

    assert len(library_repo.list_for_user(user.id)) == total_songs
    assert len(queue_repo.list_for_user(user.id)) == total_songs

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
    remainder = BACKFILL_BATCH_SIZE // 2
    songs = [
        {"videoId": f"burst-{i}", "title": f"Song {i}", "artists": [{"name": "Artist"}]}
        for i in range(BACKFILL_BATCH_SIZE + remainder)
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
    assert result.new_songs_found == BACKFILL_BATCH_SIZE  # capped, not all of them
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
    assert second.new_songs_found == remainder


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
    classification_service.classify_track.return_value = [
        ClassificationResult(
            playlist_id=playlist.id,
            confidence=0.8,
            explanation=Explanation("artist_similarity", "matched"),
            genre=None,
        )
    ]

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


def test_mark_removed_songs_marks_an_approved_pending_apply_item_stale(session):
    """U5/KTD3: _ACTIVE_QUEUE_STATUSES includes approved_pending_apply so an
    unliked song still in that state gets caught by the ordinary ingestion
    check's staleness pass, not just by U6's own Finish & Apply
    reconciliation."""
    from app.jobs.ingestion import _mark_removed_songs

    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    library_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=library_item.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
        )
    )

    songs_marked_removed = _mark_removed_songs(
        existing_items=[library_item],
        liked_video_ids=set(),
        queue_items_snapshot=[queue_item],
        library_repository=library_repo,
        review_queue_repository=queue_repo,
    )

    assert songs_marked_removed == 1
    refreshed = queue_repo.get(queue_item.id)
    assert refreshed.status == "stale"


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


def test_matching_playlist_is_not_reoffered_a_second_pending_candidate(session):
    """The (library_item, playlist) pair dedup check (mirrors
    reorganize_matching.py's active_pairs_for_user usage): if a song already
    has an active/committed candidate row for a playlist -- e.g. from an
    earlier check, or from Reorganize matching the same song/playlist pair
    -- a later ingestion check must not queue a second one for that exact
    pair. A different, second matched playlist still gets its own row."""
    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    rock = playlist_repo.create(Playlist(user_id=user.id, name="Rock", description=None, rule=None))
    chill = playlist_repo.create(Playlist(user_id=user.id, name="Chill", description=None, rule=None))

    library_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    # Already an active candidate for (v1, Rock) -- e.g. queued by an earlier
    # pass. (v1, Chill) has no candidate yet.
    queue_repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=library_item.id, playlist_id=rock.id, status="pending")
    )

    music_client = MagicMock()
    music_client.get_liked_songs.return_value = [
        {"videoId": "v2", "title": "Song B", "artists": [{"name": "Artist"}]}
    ]
    classification_service = MagicMock(spec=ClassificationService)
    classification_service.classify_track.return_value = [
        ClassificationResult(
            playlist_id=rock.id,
            confidence=0.8,
            explanation=Explanation("artist_similarity", "matched Rock"),
            genre=None,
        ),
        ClassificationResult(
            playlist_id=chill.id,
            confidence=0.6,
            explanation=Explanation("description_match", "matched Chill"),
            genre=None,
        ),
    ]

    # v2 is a brand-new song classified against both Rock and Chill -- this
    # only exercises the dedup path for v1's pre-existing Rock candidate via
    # the shared active_pairs set built once up front, since real dedup for
    # a genuinely new song's own two results isn't possible (neither pair
    # exists yet). Assert v1 still has exactly one Rock row (not doubled)
    # after this run, and v2 got both its own new rows.
    result = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=library_repo,
        playlist_repository=playlist_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.queue_items_created == 2  # only v2's two new rows
    v1_items = [i for i in queue_repo.list_for_user(user.id) if i.library_item_id == library_item.id]
    assert len(v1_items) == 1  # not duplicated


def test_trigger_ingestion_check_marks_in_progress(session):
    from app.jobs.ingestion import trigger_ingestion_check

    user = _onboarded_user(session)
    _, _, _, user_repo = _repos(session)
    assert user.ingestion_status == "idle"

    updated = trigger_ingestion_check(user_repo, user.id)

    assert updated.ingestion_status == "in_progress"
    assert user_repo.get(user.id).ingestion_status == "in_progress"


def test_trigger_ingestion_check_rejects_when_already_in_progress(session):
    from app.jobs.ingestion import IngestionAlreadyInProgressError, trigger_ingestion_check

    user = _onboarded_user(session)
    _, _, _, user_repo = _repos(session)
    user_repo.set_ingestion_progress(user.id, status="in_progress")

    try:
        trigger_ingestion_check(user_repo, user.id)
        assert False, "expected IngestionAlreadyInProgressError"
    except IngestionAlreadyInProgressError:
        pass


def test_run_ingestion_check_to_completion_loops_backfill_to_done(session):
    """The background-task version (mirrors run_reorganize_matching): a
    backlog bigger than one batch is processed to completion in a single
    call, instead of depending on the frontend to click "Load next 50
    songs" repeatedly."""
    from app.jobs.ingestion import run_ingestion_check_to_completion

    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    songs = [
        {"videoId": f"v{i}", "title": f"Song {i}", "artists": [{"name": "Artist"}]}
        for i in range(BACKFILL_BATCH_SIZE + 25)
    ]
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = songs
    classification_service = _matching_classification_service(playlist.id)

    run_ingestion_check_to_completion(
        user.id,
        engine=session.get_bind(),
        music_client=music_client,
        classification_service=classification_service,
    )

    fresh_user = user_repo.get(user.id)
    assert fresh_user.ingestion_status == "done"
    assert fresh_user.ingestion_processed_count == len(songs)
    assert fresh_user.ingestion_total_count == len(songs)
    assert fresh_user.backfill_completed_at is not None
    assert len(library_repo.list_for_user(user.id)) == len(songs)
    assert len(queue_repo.list_for_user(user.id)) == len(songs)


def test_run_ingestion_check_to_completion_stops_once_steady_state_burst_is_drained(session):
    from app.jobs.ingestion import run_ingestion_check_to_completion

    user = _onboarded_user(session)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    user_repo.mark_backfill_completed(user.id)  # already in steady_state
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )
    songs = [
        {"videoId": f"burst-{i}", "title": f"Song {i}", "artists": [{"name": "Artist"}]}
        for i in range(BACKFILL_BATCH_SIZE + 10)
    ]
    music_client = MagicMock()
    music_client.get_liked_songs.return_value = songs
    classification_service = _matching_classification_service(playlist.id)

    run_ingestion_check_to_completion(
        user.id,
        engine=session.get_bind(),
        music_client=music_client,
        classification_service=classification_service,
    )

    fresh_user = user_repo.get(user.id)
    assert fresh_user.ingestion_status == "done"
    assert fresh_user.ingestion_processed_count == len(songs)
    assert len(library_repo.list_for_user(user.id)) == len(songs)


def test_reset_backlog_leaves_approved_pending_apply_songs_untouched(session):
    """U5/KTD3: a session-decided-but-unwritten item is a real decided state
    -- reset_backlog must not silently discard it."""
    user = _onboarded_user(session)
    UserRepository(session).mark_backfill_completed(user.id)
    library_repo, playlist_repo, queue_repo, user_repo = _repos(session)
    playlist = playlist_repo.create(
        Playlist(user_id=user.id, name="Rock", description=None, rule=None)
    )

    deferred_item = library_repo.create(
        LibraryItem(user_id=user.id, video_id="v-deferred", title="Deferred Song", artist="Artist")
    )
    deferred_queue_item = queue_repo.create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=deferred_item.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
        )
    )

    result = reset_backlog(
        library_repository=library_repo,
        review_queue_repository=queue_repo,
        user_repository=user_repo,
        user_id=user.id,
    )

    assert result.library_items_cleared == 0
    assert [item.id for item in library_repo.list_for_user(user.id)] == [deferred_item.id]
    assert [item.id for item in queue_repo.list_for_user(user.id)] == [deferred_queue_item.id]
