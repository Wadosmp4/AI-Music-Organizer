from unittest.mock import MagicMock, patch

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.services.library_analysis import LibraryAnalysisService


def _make_user(session) -> User:
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _add_library_items(session, user_id, songs):
    items = []
    for video_id, title, artist in songs:
        item = LibraryItem(user_id=user_id, video_id=video_id, title=title, artist=artist)
        session.add(item)
        items.append(item)
    session.commit()
    for item in items:
        session.refresh(item)
    return items


def _fake_music_client() -> MagicMock:
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.create_playlist.return_value = "yt-playlist-fake-id"
    return music_client


def _service(session, openrouter_api_key="or-key", music_client=None) -> LibraryAnalysisService:
    return LibraryAnalysisService(
        library_repository=LibraryRepository(session),
        playlist_repository=PlaylistRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        user_repository=UserRepository(session),
        music_client=music_client or _fake_music_client(),
        reorganize_session_repository=ReorganizeSessionRepository(session),
        genre_lookup=None,
        openrouter_api_key=openrouter_api_key,
    )


def _mock_cluster_response(name: str, theme: str, indices: list[int]):
    resp = MagicMock()
    resp.choices[0].message.content = (
        '{"playlists": [{"name": "%s", "theme": "%s", "indices": %s}]}' % (name, theme, indices)
    )
    return resp


def test_cluster_of_related_unplaced_songs_produces_a_new_playlist_proposal(session):
    user = _make_user(session)
    _add_library_items(
        session,
        user.id,
        [
            ("v1", "Song 1", "Artist A"),
            ("v2", "Song 2", "Artist B"),
            ("v3", "Song 3", "Artist C"),
            ("v4", "Song 4", "Artist D"),
        ],
    )
    service = _service(session)

    mock_response = _mock_cluster_response("Chill Electronic", "Laid-back electronic songs", [0, 1, 2, 3])
    with patch("app.services.library_analysis.completion", return_value=mock_response):
        proposals = service.propose_new_playlists(user.id)

    assert len(proposals) == 1
    assert proposals[0].name == "Chill Electronic"
    assert proposals[0].theme == "Laid-back electronic songs"
    assert proposals[0].song_count == 4


def test_cluster_success_surfaces_llm_clustering_health_as_ok(session):
    user = _make_user(session)
    _add_library_items(
        session,
        user.id,
        [
            ("v1", "Song 1", "Artist A"),
            ("v2", "Song 2", "Artist B"),
            ("v3", "Song 3", "Artist C"),
            ("v4", "Song 4", "Artist D"),
        ],
    )
    dependency_health_store.set_status("llm_clustering", DependencyStatus.DEGRADED, "stale")
    service = _service(session)

    mock_response = _mock_cluster_response("Chill", "chill songs", [0, 1, 2, 3])
    with patch("app.services.library_analysis.completion", return_value=mock_response):
        service.propose_new_playlists(user.id)

    status, _ = dependency_health_store.get_status("llm_clustering")
    assert status == DependencyStatus.OK


def test_cluster_batch_empty_choices_list_surfaces_degraded_health_instead_of_raising(session):
    """An empty `choices` list (e.g. a safety-filtered response) raises
    IndexError on resp.choices[0] — previously outside the narrow except
    tuple, so it would have escaped _cluster_batch entirely instead of being
    treated like any other per-batch clustering failure (KTD18)."""
    user = _make_user(session)
    _add_library_items(
        session,
        user.id,
        [
            ("v1", "Song 1", "Artist A"),
            ("v2", "Song 2", "Artist B"),
            ("v3", "Song 3", "Artist C"),
            ("v4", "Song 4", "Artist D"),
        ],
    )
    service = _service(session)

    empty_choices_response = MagicMock()
    empty_choices_response.choices = []
    with patch("app.services.library_analysis.completion", return_value=empty_choices_response):
        proposals = service.propose_new_playlists(user.id)

    assert proposals == []
    status, reason = dependency_health_store.get_status("llm_clustering")
    assert status == DependencyStatus.DEGRADED
    assert "clustering" in reason.lower()


def test_clustering_considers_songs_already_matched_to_an_existing_playlist(session):
    """Suggestions previously only looked at still-unplaced songs, so once a
    song matched an existing playlist it could never surface in a new-
    playlist proposal — even though multi-label matching means accepting a
    new proposal wouldn't remove it from where it already landed. Now the
    full accumulated library feeds clustering, not just the leftovers."""
    user = _make_user(session)
    playlist = PlaylistRepository(session).create(
        Playlist(user_id=user.id, name="Existing", description="some existing playlist", rule=None)
    )
    items = _add_library_items(
        session,
        user.id,
        [
            ("v1", "Song 1", "Artist A"),
            ("v2", "Song 2", "Artist B"),
            ("v3", "Song 3", "Artist C"),
            ("v4", "Song 4", "Artist D"),
        ],
    )
    # Song 1 already matched "Existing" and is sitting pending review there —
    # a settled placement, but not a reason to exclude it from clustering.
    ReviewQueueRepository(session).create(
        ReviewQueueItem(user_id=user.id, library_item_id=items[0].id, playlist_id=playlist.id)
    )
    service = _service(session)

    mock_response = _mock_cluster_response("Chill Electronic", "Laid-back electronic songs", [0, 1, 2, 3])
    with patch("app.services.library_analysis.completion", return_value=mock_response):
        proposals = service.propose_new_playlists(user.id)

    assert len(proposals) == 1
    assert proposals[0].song_count == 4


def test_clustering_excludes_removed_songs_no_longer_in_the_library(session):
    user = _make_user(session)
    items = _add_library_items(
        session,
        user.id,
        [
            ("v1", "Song 1", "Artist A"),
            ("v2", "Song 2", "Artist B"),
            ("v3", "Song 3", "Artist C"),
            ("v4", "Song 4", "Artist D"),
        ],
    )
    LibraryRepository(session).mark_removed(items[0].id)
    service = _service(session)

    mock_response = _mock_cluster_response("Chill Electronic", "Laid-back electronic songs", [0, 1, 2])
    with patch("app.services.library_analysis.completion", return_value=mock_response) as mock_completion:
        proposals = service.propose_new_playlists(user.id)

    _, kwargs = mock_completion.call_args
    user_message = kwargs["messages"][1]["content"]
    assert "Song 1" not in user_message
    assert proposals == []  # only 3 remaining songs, below MIN_CLUSTER_SIZE


def test_existing_playlist_style_is_passed_as_context_to_the_clustering_prompt(session):
    """Clustering previously only saw artist/title/genre per song, which left
    it defaulting to same-artist bins when the genre tag was missing or too
    generic to differentiate. Passing the user's own already-added playlist
    names/descriptions calibrates the LLM toward the genre/mood-spanning-
    multiple-artists style the user already organizes by."""
    user = _make_user(session)
    PlaylistRepository(session).create(
        Playlist(
            user_id=user.id,
            name="EDM mix",
            description="High-energy electronic dance music and remixes.",
            rule=None,
        )
    )
    _add_library_items(
        session,
        user.id,
        [
            ("v1", "Song 1", "Artist A"),
            ("v2", "Song 2", "Artist B"),
            ("v3", "Song 3", "Artist C"),
            ("v4", "Song 4", "Artist D"),
        ],
    )
    service = _service(session)

    mock_response = _mock_cluster_response("Chill Electronic", "Laid-back electronic songs", [0, 1, 2, 3])
    with patch("app.services.library_analysis.completion", return_value=mock_response) as mock_completion:
        service.propose_new_playlists(user.id)

    _, kwargs = mock_completion.call_args
    user_message = kwargs["messages"][1]["content"]
    assert "EDM mix" in user_message
    assert "High-energy electronic dance music and remixes." in user_message


def test_clustering_prompt_discourages_same_artist_groupings(session):
    from app.services.library_analysis import SYSTEM_PROMPT

    assert "never by artist identity" in SYSTEM_PROMPT.lower() or "not by artist" in SYSTEM_PROMPT.lower()


def test_added_playlists_shown_for_context_but_not_modified(session):
    user = _make_user(session)
    existing = PlaylistRepository(session).create(
        Playlist(user_id=user.id, name="My Playlist", description="already here", rule=None)
    )
    service = _service(session)

    listed = service.list_added_playlists(user.id)

    assert len(listed) == 1
    assert listed[0].id == existing.id
    assert listed[0].name == "My Playlist"
    refreshed = PlaylistRepository(session).get(existing.id)
    assert refreshed.name == "My Playlist"
    assert refreshed.description == "already here"


def test_existing_youtube_playlists_excludes_ones_already_added_by_this_app(session):
    user = _make_user(session)
    PlaylistRepository(session).create(
        Playlist(
            user_id=user.id,
            name="App-Created",
            description=None,
            rule=None,
            youtube_playlist_id="yt-already-added",
        )
    )
    music_client = _fake_music_client()
    music_client.get_library_playlists.return_value = [
        {"playlistId": "yt-already-added", "title": "App-Created"},
        {"playlistId": "yt-pre-existing", "title": "Road Trip"},
    ]
    service = _service(session, music_client=music_client)

    listed = service.list_existing_youtube_playlists(user.id)

    assert listed == [{"playlistId": "yt-pre-existing", "title": "Road Trip"}]


def test_existing_youtube_playlists_degrades_to_empty_list_on_failure(session):
    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_library_playlists.side_effect = RuntimeError("needs reconnect")
    service = _service(session, music_client=music_client)

    assert service.list_existing_youtube_playlists(user.id) == []


def test_selecting_a_proposed_candidate_creates_an_empty_playlist_with_no_songs(session):
    user = _make_user(session)
    service = _service(session)

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[{"name": "Chill Electronic", "theme": "Laid-back electronic songs"}],
        custom_playlists=[],
    )

    assert len(created) == 1
    playlist = PlaylistRepository(session).get(created[0].id)
    assert playlist.name == "Chill Electronic"
    assert playlist.description == "Laid-back electronic songs"
    assert playlist.youtube_playlist_id == "yt-playlist-fake-id"
    assert ReviewQueueRepository(session).list_for_user(user.id) == []


def test_custom_playlist_added_during_onboarding_creates_an_empty_playlist_the_same_way(session):
    user = _make_user(session)
    service = _service(session)

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[{"name": "Road Trip", "description": "upbeat driving songs"}],
    )

    assert len(created) == 1
    playlist = PlaylistRepository(session).get(created[0].id)
    assert playlist.name == "Road Trip"
    assert playlist.description == "upbeat driving songs"
    assert playlist.youtube_playlist_id == "yt-playlist-fake-id"
    assert ReviewQueueRepository(session).list_for_user(user.id) == []


def test_adopting_an_existing_youtube_playlist_creates_a_local_record_without_recreating_it(
    session,
):
    user = _make_user(session)
    music_client = _fake_music_client()
    service = _service(session, music_client=music_client)

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        adopted_playlists=[{"playlist_id": "yt-pre-existing", "name": "Road Trip"}],
    )

    assert len(created) == 1
    playlist = PlaylistRepository(session).get(created[0].id)
    assert playlist.name == "Road Trip"
    assert playlist.youtube_playlist_id == "yt-pre-existing"
    music_client.create_playlist.assert_not_called()


def test_unchecking_an_already_added_playlist_removes_it_without_touching_youtube(session):
    user = _make_user(session)
    music_client = _fake_music_client()
    service = _service(session, music_client=music_client)
    playlist = PlaylistRepository(session).create(
        Playlist(
            user_id=user.id,
            name="Workout",
            description=None,
            rule=None,
            youtube_playlist_id="yt-workout",
        )
    )

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        removed_playlist_ids=[playlist.id],
    )

    assert created == []
    assert PlaylistRepository(session).list_for_user(user.id) == []
    music_client.create_playlist.assert_not_called()


def test_removing_a_playlist_clears_dangling_review_queue_references(session):
    user = _make_user(session)
    service = _service(session)
    playlist = PlaylistRepository(session).create(
        Playlist(
            user_id=user.id,
            name="Workout",
            description=None,
            rule=None,
            youtube_playlist_id="yt-workout",
        )
    )
    library_item = LibraryRepository(session).create(
        LibraryItem(user_id=user.id, video_id="v1", title="Song A", artist="Artist")
    )
    queue_repo = ReviewQueueRepository(session)
    queue_item = queue_repo.create(
        ReviewQueueItem(user_id=user.id, library_item_id=library_item.id, playlist_id=playlist.id)
    )

    service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        removed_playlist_ids=[playlist.id],
    )

    refreshed = queue_repo.get(queue_item.id)
    assert refreshed.playlist_id is None


def test_removing_a_playlist_belonging_to_another_user_is_ignored(session):
    user = _make_user(session)
    other_user = _make_user(session)
    service = _service(session)
    other_playlist = PlaylistRepository(session).create(
        Playlist(
            user_id=other_user.id,
            name="Someone Else's",
            description=None,
            rule=None,
            youtube_playlist_id="yt-other",
        )
    )

    service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        removed_playlist_ids=[other_playlist.id],
    )

    assert PlaylistRepository(session).get(other_playlist.id) is not None


def test_rejecting_a_proposed_candidate_creates_nothing(session):
    user = _make_user(session)
    service = _service(session)

    # Rejection is simply never including the candidate in accepted_proposals.
    created = service.complete_onboarding(user_id=user.id, accepted_proposals=[], custom_playlists=[])

    assert created == []
    assert PlaylistRepository(session).list_for_user(user.id) == []


# -- U2: full-library fetch + progressive suggestion clustering ------------


def _fake_liked_songs(video_ids: list[str]) -> list[dict]:
    return [
        {"videoId": vid, "title": f"Song {vid}", "artists": [{"name": f"Artist {vid}"}]}
        for vid in video_ids
    ]


def test_trigger_reorganize_creates_a_session_when_none_exists(session):
    from app.repositories.reorganize_session_repository import ReorganizeSessionRepository

    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1", "v2"])
    service = _service(session, music_client=music_client)

    reorganize_session, liked_songs = service.trigger_reorganize(user.id)

    assert reorganize_session.clustering_status == "in_progress"
    assert set(reorganize_session.video_id_snapshot) == {"v1", "v2"}
    assert len(liked_songs) == 2
    assert ReorganizeSessionRepository(session).get(reorganize_session.id) is not None


def test_second_trigger_reuses_the_still_unresolved_session(session):
    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1"])
    service = _service(session, music_client=music_client)

    first_session, _ = service.trigger_reorganize(user.id)

    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1", "v2"])
    second_session, _ = service.trigger_reorganize(user.id)

    assert second_session.id == first_session.id
    assert set(second_session.video_id_snapshot) == {"v1", "v2"}


def test_reorganize_clustering_persists_proposals_incrementally_and_marks_done(session):
    from app.services.library_analysis import run_reorganize_clustering

    user = _make_user(session)
    music_client = _fake_music_client()
    liked_songs = _fake_liked_songs(["v1", "v2", "v3", "v4"])
    music_client.get_liked_songs.return_value = liked_songs
    service = _service(session, music_client=music_client)

    reorganize_session, fetched_songs = service.trigger_reorganize(user.id)
    assert service.get_reorganize_status(reorganize_session.id).clustering_status == "in_progress"

    mock_response = _mock_cluster_response("Chill Electronic", "Laid-back electronic songs", [0, 1, 2, 3])
    with patch("app.services.library_analysis.completion", return_value=mock_response):
        run_reorganize_clustering(reorganize_session.id, fetched_songs, engine=session.get_bind())

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.clustering_status == "done"
    assert len(status.proposals) == 1
    assert status.proposals[0].name == "Chill Electronic"
    assert status.proposals[0].song_count == 4


def test_reorganize_clustering_batch_failure_does_not_block_other_batches(session):
    """Mirrors _cluster_batch's own broad-except path (KTD18): one batch's
    LLM failure just leaves its songs unclustered for this pass, it doesn't
    stop later batches from completing and persisting their own proposals."""
    from app.services.library_analysis import BATCH_SIZE, run_reorganize_clustering

    user = _make_user(session)
    music_client = _fake_music_client()
    video_ids = [f"v{i}" for i in range(BATCH_SIZE + 4)]
    liked_songs = _fake_liked_songs(video_ids)
    music_client.get_liked_songs.return_value = liked_songs
    service = _service(session, music_client=music_client)

    reorganize_session, fetched_songs = service.trigger_reorganize(user.id)

    ok_response = _mock_cluster_response("Second Batch", "theme", [0, 1, 2, 3])
    responses = [RuntimeError("LLM failure"), ok_response]

    def _fake_completion(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("app.services.library_analysis.completion", side_effect=_fake_completion):
        run_reorganize_clustering(reorganize_session.id, fetched_songs, engine=session.get_bind())

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.clustering_status == "done"
    assert len(status.proposals) == 1
    assert status.proposals[0].name == "Second Batch"


def test_reorganize_status_reports_stalled_when_no_recent_proposal_activity(session):
    from datetime import timedelta

    from app.models.base import utcnow
    from app.repositories.reorganize_session_repository import ReorganizeSessionRepository

    user = _make_user(session)
    service = _service(session)

    reorganize_session_repo = ReorganizeSessionRepository(session)
    reorganize_session, _ = service.trigger_reorganize(user.id)
    reorganize_session.updated_at = utcnow() - timedelta(seconds=999)
    reorganize_session_repo.update(reorganize_session)

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.clustering_status == "stalled"


def test_onboarding_completion_is_gated_until_selection_finishes(session):
    user = _make_user(session)
    service = _service(session)

    fresh = UserRepository(session).get(user.id)
    assert fresh.onboarding_completed_at is None  # U4's backfill must not start yet

    service.complete_onboarding(user_id=user.id, accepted_proposals=[], custom_playlists=[])

    completed = UserRepository(session).get(user.id)
    assert completed.onboarding_completed_at is not None
