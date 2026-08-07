from unittest.mock import MagicMock, patch

import pytest

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.bpm_lookup import BpmLookupResult, BpmLookupService
from app.services.classification import ClassificationResult, ClassificationService, Explanation
from app.services.genre_lookup import GenreLookupService
from app.services.playlist_creation import PlaylistCreationService


@pytest.fixture(autouse=True)
def reset_dependency_health():
    dependency_health_store.set_status("lastfm", DependencyStatus.OK)
    dependency_health_store.set_status("getsongbpm", DependencyStatus.OK)
    dependency_health_store.set_status("llm_bpm_estimate", DependencyStatus.OK)
    dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)
    dependency_health_store.set_status("llm_clustering", DependencyStatus.OK)
    yield


def _make_user(session) -> User:
    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _fake_music_client() -> MagicMock:
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.create_playlist.return_value = "yt-playlist-fake-id"
    return music_client


def _service(session, classification_service, music_client=None) -> PlaylistCreationService:
    return PlaylistCreationService(
        playlist_repository=PlaylistRepository(session),
        library_repository=LibraryRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        classification_service=classification_service,
        music_client=music_client or _fake_music_client(),
    )


def _no_match_classification_service() -> MagicMock:
    classification_service = MagicMock(spec=ClassificationService)
    classification_service.classify_track.return_value = ClassificationResult(
        playlist_id=None,
        confidence=0.0,
        explanation=Explanation("none", "no match"),
        bpm=None,
        bpm_source=None,
        genre=None,
    )
    return classification_service


def test_description_persists_and_is_retrievable_after_creation(session):
    user = _make_user(session)
    service = _service(session, _no_match_classification_service())

    result = service.create_playlist_and_propose_matches(
        user_id=user.id,
        name="Late Night Chill",
        description="mellow, slow acoustic songs for winding down",
    )

    assert result.playlist.id is not None
    assert result.review_queue_items_created == 0

    fetched = PlaylistRepository(session).get(result.playlist.id)
    assert fetched is not None
    assert fetched.description == "mellow, slow acoustic songs for winding down"
    assert fetched.rule is None
    # A playlist without a youtube_playlist_id can never be approved/moved
    # into (ReviewQueueService._youtube_playlist_id would 404 every time).
    assert fetched.youtube_playlist_id == "yt-playlist-fake-id"


def test_persisted_description_matches_a_later_ingestion_tick(session):
    """The description must be a durable property of the playlist row, not
    something only consulted at creation time (KTD11): create the playlist
    while the library is empty (no matches possible yet), then simulate a
    later ingestion tick — a new liked song arrives and gets matched purely
    from the description fetched back off the Playlist row.
    """
    user = _make_user(session)

    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    bpm_lookup.openrouter_api_key = "or-key"
    classification_service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")
    service = _service(session, classification_service)

    result = service.create_playlist_and_propose_matches(
        user_id=user.id,
        name="Late Night Chill",
        description="mellow, slow acoustic songs for winding down",
    )
    assert result.review_queue_items_created == 0  # nothing in the library yet

    # A later ingestion tick: a new song gets liked after the playlist exists.
    library_item = LibraryItem(
        user_id=user.id, video_id="new-song-1", title="Quiet Room", artist="Some Artist"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    # Re-fetch the playlist fresh from the DB (not the in-memory object handed
    # back at creation) to prove the match is driven by the persisted column.
    persisted_playlist = PlaylistRepository(session).get(result.playlist.id)

    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"matched_playlists": ["Late Night Chill"]}'
    with patch("app.services.classification.completion", return_value=mock_response):
        created_count = service.propose_matches(user.id, persisted_playlist)

    assert created_count == 1
    items = ReviewQueueRepository(session).list_for_user(user.id)
    assert len(items) == 1
    assert items[0].playlist_id == persisted_playlist.id
    assert items[0].library_item_id == library_item.id
    assert items[0].status == "pending"  # never auto-approved (R8/R11)
    assert items[0].explanation["signal"] == "description_match"


def test_classify_track_failure_for_one_song_does_not_block_the_rest_of_the_batch(session):
    """Mirrors jobs/ingestion.py's identical guard (KTD18): propose_matches
    previously had no per-song try/except, so one song's classify_track
    exception would abort matching for the whole remaining backlog and
    surface as an unhandled 500 from the playlists route."""
    user = _make_user(session)
    playlist = PlaylistRepository(session).create(
        Playlist(
            user_id=user.id,
            name="Chill",
            description="chill songs",
            rule=None,
            youtube_playlist_id="PL1",
        )
    )
    failing_item = LibraryItem(user_id=user.id, video_id="v-fail", title="Bad Song", artist="Artist")
    matching_item = LibraryItem(user_id=user.id, video_id="v-match", title="Good Song", artist="Artist")
    session.add(failing_item)
    session.add(matching_item)
    session.commit()
    session.refresh(failing_item)
    session.refresh(matching_item)

    def _classify(track, candidates, user_id):
        if track["videoId"] == "v-fail":
            raise RuntimeError("classification boom")
        return [
            ClassificationResult(
                playlist_id=playlist.id,
                confidence=0.8,
                explanation=Explanation("description_match", "matched"),
                bpm=None,
                bpm_source=None,
                genre=None,
            )
        ]

    classification_service = MagicMock(spec=ClassificationService)
    classification_service.classify_track.side_effect = _classify
    service = _service(session, classification_service)

    created_count = service.propose_matches(user.id, playlist)

    assert created_count == 1  # v-match still got its review_queue item
    items = ReviewQueueRepository(session).list_for_user(user.id)
    assert len(items) == 1
    assert items[0].library_item_id == matching_item.id


def test_rule_gated_existing_playlist_composes_independently_of_new_described_playlist(session):
    """KTD10 hard-gate precedence must still hold once a newly-created,
    description-only playlist is added to the candidate mix: an existing
    ruled playlist whose rule this song fails must not somehow route the
    song to it, and must not block the new playlist's own description match
    either — the two playlists' candidacy is decided independently.
    """
    user = _make_user(session)

    ruled_playlist = PlaylistRepository(session).create(
        Playlist(
            user_id=user.id,
            name="High Tempo",
            description="fast workout tracks",
            rule={"bpm_min": 150},
        )
    )

    library_item = LibraryItem(
        user_id=user.id, video_id="song-1", title="Slow Ballad", artist="Someone"
    )
    session.add(library_item)
    session.commit()
    session.refresh(library_item)

    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=80.0, source="measured")  # fails the 150bpm rule
    bpm_lookup.openrouter_api_key = "or-key"
    classification_service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")
    service = _service(session, classification_service)

    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"matched_playlists": ["Wind Down"]}'
    with patch("app.services.classification.completion", return_value=mock_response):
        result = service.create_playlist_and_propose_matches(
            user_id=user.id, name="Wind Down", description="slow, relaxing songs"
        )

    assert result.review_queue_items_created == 1
    items = ReviewQueueRepository(session).list_for_user(user.id)
    assert len(items) == 1
    assert items[0].playlist_id == result.playlist.id
    assert items[0].explanation["signal"] == "description_match"

    # The rule-gated playlist got nothing for this song: its rule (bpm_min=150)
    # rejected the 80bpm song outright, and — per KTD10 — that rejection isn't
    # reconsidered via the new playlist's description on its own behalf either.
    ruled_playlist_items = [i for i in items if i.playlist_id == ruled_playlist.id]
    assert ruled_playlist_items == []
