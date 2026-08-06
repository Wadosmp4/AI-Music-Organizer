from unittest.mock import MagicMock, patch

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
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


def test_existing_playlists_shown_for_context_but_not_modified(session):
    user = _make_user(session)
    existing = PlaylistRepository(session).create(
        Playlist(user_id=user.id, name="My Playlist", description="already here", rule=None)
    )
    service = _service(session)

    listed = service.list_existing_playlists(user.id)

    assert len(listed) == 1
    assert listed[0].id == existing.id
    assert listed[0].name == "My Playlist"
    refreshed = PlaylistRepository(session).get(existing.id)
    assert refreshed.name == "My Playlist"
    assert refreshed.description == "already here"


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


def test_rejecting_a_proposed_candidate_creates_nothing(session):
    user = _make_user(session)
    service = _service(session)

    # Rejection is simply never including the candidate in accepted_proposals.
    created = service.complete_onboarding(user_id=user.id, accepted_proposals=[], custom_playlists=[])

    assert created == []
    assert PlaylistRepository(session).list_for_user(user.id) == []


def test_onboarding_completion_is_gated_until_selection_finishes(session):
    user = _make_user(session)
    service = _service(session)

    fresh = UserRepository(session).get(user.id)
    assert fresh.onboarding_completed_at is None  # U4's backfill must not start yet

    service.complete_onboarding(user_id=user.id, accepted_proposals=[], custom_playlists=[])

    completed = UserRepository(session).get(user.id)
    assert completed.onboarding_completed_at is not None
