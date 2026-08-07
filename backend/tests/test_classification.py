from unittest.mock import MagicMock, patch

import pytest
import requests

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.playlist import Playlist
from app.repositories.playlist_repository import PlaylistRepository
from app.services.bpm_lookup import BpmLookupResult, BpmLookupService
from app.services.classification import (
    CandidatePlaylist,
    ClassificationService,
    build_artist_counts,
    build_candidate,
)
from app.services.genre_lookup import GenreLookupService


@pytest.fixture(autouse=True)
def reset_dependency_health():
    dependency_health_store.set_status("lastfm", DependencyStatus.OK)
    dependency_health_store.set_status("getsongbpm", DependencyStatus.OK)
    dependency_health_store.set_status("llm_bpm_estimate", DependencyStatus.OK)
    dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)
    dependency_health_store.set_status("llm_clustering", DependencyStatus.OK)
    yield


def _service(genre=None, bpm_result=None):
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = genre
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = bpm_result or BpmLookupResult(bpm=None, source=None)
    bpm_lookup.openrouter_api_key = None
    return ClassificationService(genre_lookup, bpm_lookup)


def test_every_suggestion_carries_a_confidence_score():
    playlist = CandidatePlaylist(id=1, name="Rock", rule=None, description=None, artist_counts={"queen": 5})
    service = _service()

    results = service.classify_track({"title": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]}, [playlist])

    assert len(results) == 1
    assert isinstance(results[0].confidence, float)
    assert 0.0 <= results[0].confidence <= 1.0


def test_bpm_lookup_hit_returns_measured_value():
    bpm_service = BpmLookupService(api_key="test-key", openrouter_api_key=None)
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {"search": [{"tempo": "128"}]}

    with patch("app.services.bpm_lookup.requests.get", return_value=fake_response):
        result = bpm_service.lookup_bpm("Artist", "Title")

    assert result.bpm == 128.0
    assert result.source == "measured"


def test_bpm_miss_falls_back_to_flagged_llm_estimate():
    bpm_service = BpmLookupService(api_key="test-key", openrouter_api_key="or-key")
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {"search": []}

    mock_completion_resp = MagicMock()
    mock_completion_resp.choices[0].message.content = '{"bpm": 140.0}'

    with patch("app.services.bpm_lookup.requests.get", return_value=fake_response):
        with patch("app.services.bpm_lookup.completion", return_value=mock_completion_resp):
            result = bpm_service.lookup_bpm("Artist", "Title")

    assert result.bpm == 140.0
    assert result.source == "estimated"


def test_bpm_rate_limit_is_distinguishable_from_genuine_miss():
    # openrouter_api_key="" explicitly disables the LLM fallback — passing None
    # would fall through to get_settings() and could pick up a real key from
    # the process environment, making a real paid API call during a test.
    bpm_service = BpmLookupService(api_key="test-key", openrouter_api_key="")
    rate_limited_response = MagicMock(status_code=429)

    with patch("app.services.bpm_lookup.requests.get", return_value=rate_limited_response):
        result = bpm_service.lookup_bpm("Artist", "Title")

    assert result.bpm is None
    status, reason = dependency_health_store.get_status("getsongbpm")
    assert status == DependencyStatus.DEGRADED
    assert "rate limit" in reason.lower()


def test_bpm_network_timeout_surfaces_degraded_health():
    bpm_service = BpmLookupService(api_key="test-key", openrouter_api_key="")

    with patch("app.services.bpm_lookup.requests.get", side_effect=requests.Timeout("timed out")):
        result = bpm_service.lookup_bpm("Artist", "Title")

    assert result.bpm is None
    status, reason = dependency_health_store.get_status("getsongbpm")
    assert status == DependencyStatus.DEGRADED
    assert "timed out" in reason.lower() or "failed" in reason.lower()


def test_bpm_tripped_circuit_breaker_is_distinguishable_from_genuine_miss():
    """CircuitOpenError is raised by call_with_retry's circuit_breaker.before_call(),
    not by the request itself — it must be caught alongside requests.RequestException
    or it escapes _lookup_measured uncaught (KTD18)."""
    bpm_service = BpmLookupService(api_key="test-key", openrouter_api_key="")
    for _ in range(bpm_service._circuit_breaker.failure_threshold):
        bpm_service._circuit_breaker.record_failure()

    result = bpm_service.lookup_bpm("Artist", "Title")

    assert result.bpm is None
    status, reason = dependency_health_store.get_status("getsongbpm")
    assert status == DependencyStatus.DEGRADED
    assert "circuit" in reason.lower()


def test_bpm_malformed_tempo_field_surfaces_degraded_health_instead_of_raising():
    bpm_service = BpmLookupService(api_key="test-key", openrouter_api_key="")
    fake_response = MagicMock(status_code=200)
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"search": [{"tempo": "not-a-number"}]}

    with patch("app.services.bpm_lookup.requests.get", return_value=fake_response):
        result = bpm_service.lookup_bpm("Artist", "Title")

    assert result.bpm is None
    status, reason = dependency_health_store.get_status("getsongbpm")
    assert status == DependencyStatus.DEGRADED
    assert "tempo" in reason.lower()


def test_rule_gated_playlist_ignores_description_only_match():
    ruled = CandidatePlaylist(
        id=1, name="High Energy", rule={"bpm_min": 150}, description="chill background music", artist_counts={}
    )
    service = _service(bpm_result=BpmLookupResult(bpm=90.0, source="measured"))

    results = service.classify_track({"title": "Slow Song", "artists": [{"name": "Someone"}]}, [ruled])

    # The rule (bpm_min=150) rejects this 90bpm song outright; its description
    # ("chill background music") must not be used to route it there anyway (KTD10).
    assert len(results) == 1
    assert results[0].playlist_id is None
    assert results[0].explanation.signal == "none"


def test_explanation_names_the_rule_signal():
    ruled = CandidatePlaylist(id=1, name="Rock Only", rule={"genre": "rock"}, description=None, artist_counts={})
    service = _service(genre="rock")

    results = service.classify_track({"title": "Song", "artists": [{"name": "Band"}]}, [ruled])

    assert len(results) == 1
    assert results[0].explanation.signal == "rule"
    assert "Rock Only" in results[0].explanation.detail


def test_explanation_names_the_artist_similarity_signal():
    playlist = CandidatePlaylist(
        id=1, name="Queen Hits", rule=None, description=None, artist_counts={"queen": 5}
    )
    service = _service()

    results = service.classify_track({"title": "Song", "artists": [{"name": "Queen"}]}, [playlist])

    assert len(results) == 1
    assert results[0].explanation.signal == "artist_similarity"
    assert "Queen Hits" in results[0].explanation.detail


def test_single_song_llm_failure_does_not_raise_and_falls_through():
    playlist = CandidatePlaylist(
        id=1, name="Moody", rule=None, description="melancholy songs", artist_counts={}
    )
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    bpm_lookup.openrouter_api_key = "or-key"
    service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")

    with patch(
        "app.services.classification.call_with_retry", side_effect=RuntimeError("timeout")
    ):
        results = service.classify_track({"title": "Song", "artists": [{"name": "Nobody"}]}, [playlist])

    assert len(results) == 1
    assert results[0].playlist_id is None
    assert results[0].explanation.signal == "none"

    # A second, independent call still proceeds normally — one song's failure
    # doesn't leave the service or health store in a state that blocks the
    # next. Explicitly mocked (not left to hit a real LLM): completion()
    # never receives self.openrouter_api_key at all -- litellm resolves
    # credentials from the process environment -- so an unmocked call here
    # would silently hit a real API using whatever key happens to be in the
    # shell's environment instead of exercising this test's own fixture.
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"matched_playlists": []}'
    with patch("app.services.classification.completion", return_value=mock_response):
        second_results = service.classify_track(
            {"title": "Other Song", "artists": [{"name": "Nobody"}]}, [playlist]
        )
    assert second_results[0].explanation.signal == "none"


def test_golden_set_artist_similarity_routes_to_existing_themed_playlist():
    """Representative of this session's own verified routing (Aurora, Attack on
    Titan clusters landed in their existing playlists via artist-match, not a
    new one) — synthetic data here, not the real personal library."""
    aurora_playlist = CandidatePlaylist(
        id=1, name="Aurora", rule=None, description=None, artist_counts={"aurora": 6}
    )
    aot_playlist = CandidatePlaylist(
        id=2, name="Attack on Titan", rule=None, description=None, artist_counts={"linkedhorizon": 4}
    )
    service = _service()

    aurora_results = service.classify_track(
        {"title": "Runaway", "artists": [{"name": "AURORA"}]}, [aurora_playlist, aot_playlist]
    )
    aot_results = service.classify_track(
        {"title": "Guren no Yumiya", "artists": [{"name": "Linked Horizon"}]},
        [aurora_playlist, aot_playlist],
    )

    assert [r.playlist_id for r in aurora_results] == [1]
    assert [r.playlist_id for r in aot_results] == [2]


def test_genre_lookup_caches_to_the_database_not_a_file(session):
    genre_service = GenreLookupService(session, api_key="test-key")
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "toptags": {"tag": [{"name": "synthpop", "count": "80"}]}
    }
    fake_response.raise_for_status.return_value = None

    with patch("app.services.genre_lookup.requests.get", return_value=fake_response) as mock_get:
        first = genre_service.genre_for("AURORA")
        second = genre_service.genre_for("AURORA")

    assert first == second == "synthpop"
    mock_get.assert_called_once()  # second call served from the DB-backed cache (KTD23)


def test_genre_lookup_tripped_circuit_breaker_is_distinguishable_from_genuine_miss(session):
    """Same CircuitOpenError gap as bpm_lookup.py's _lookup_measured — it's
    raised by call_with_retry's circuit_breaker.before_call(), not by the
    request itself, so it must be caught alongside requests.RequestException."""
    genre_service = GenreLookupService(session, api_key="test-key")
    for _ in range(genre_service._circuit_breaker.failure_threshold):
        genre_service._circuit_breaker.record_failure()

    result = genre_service.genre_for("Some New Artist")

    assert result is None
    status, reason = dependency_health_store.get_status("lastfm")
    assert status == DependencyStatus.DEGRADED
    assert "circuit" in reason.lower()


def _playlist(playlist_id=1, youtube_playlist_id="yt-1"):
    return Playlist(
        id=playlist_id,
        user_id=1,
        name="Existing",
        description=None,
        rule=None,
        youtube_playlist_id=youtube_playlist_id,
    )


def test_build_artist_counts_tallies_the_playlists_real_youtube_content():
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.get_playlist_tracks.return_value = [
        {"videoId": "v1", "title": "Song A", "artists": [{"name": "Daft Punk"}]},
        {"videoId": "v2", "title": "Song B", "artists": [{"name": "Daft Punk"}]},
        {"videoId": "v3", "title": "Song C", "artists": [{"name": "Justice"}]},
    ]

    counts = build_artist_counts(music_client, _playlist())

    assert counts == {"daftpunk": 2, "justice": 1}
    music_client.get_playlist_tracks.assert_called_once_with("yt-1")


def test_build_artist_counts_is_empty_for_a_playlist_with_no_youtube_id():
    music_client = MagicMock(spec=MusicServiceClient)

    counts = build_artist_counts(music_client, _playlist(youtube_playlist_id=None))

    assert counts == {}
    music_client.get_playlist_tracks.assert_not_called()


def test_build_artist_counts_degrades_to_empty_on_fetch_failure():
    """KTD18: one playlist's fetch problem must not block classification
    against every other candidate."""
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.get_playlist_tracks.side_effect = RuntimeError("needs reconnect")

    counts = build_artist_counts(music_client, _playlist())

    assert counts == {}


def test_a_song_can_match_more_than_one_playlist_at_once():
    """The user's own framing: a song isn't limited to one playlist. An
    artist repeating in one playlist's history and a genuine vibe/theme fit
    in a completely different playlist are independent signals -- both
    playlists should get queued, not just whichever signal "wins"."""
    artist_playlist = CandidatePlaylist(
        id=1, name="Old Favorites", rule=None, description=None, artist_counts={"queen": 5}
    )
    vibe_playlist = CandidatePlaylist(
        id=2, name="Arena Rock Anthems", rule=None, description="stadium rock anthems", artist_counts={}
    )
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")

    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"matched_playlists": ["Arena Rock Anthems"]}'
    with patch("app.services.classification.completion", return_value=mock_response):
        results = service.classify_track(
            {"title": "Song", "artists": [{"name": "Queen"}]}, [artist_playlist, vibe_playlist]
        )

    by_playlist = {r.playlist_id: r for r in results}
    assert set(by_playlist) == {1, 2}
    assert by_playlist[1].explanation.signal == "artist_similarity"
    assert by_playlist[2].explanation.signal == "description_match"


def test_a_playlist_matched_via_description_is_not_also_reported_via_artist_similarity():
    """A playlist that already has real artist history AND a description fit
    for the same song must appear exactly once in the results, not twice."""
    playlist = CandidatePlaylist(
        id=1, name="Arena Rock Anthems", rule=None, description="stadium rock anthems", artist_counts={"queen": 5}
    )
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")

    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"matched_playlists": ["Arena Rock Anthems"]}'
    with patch("app.services.classification.completion", return_value=mock_response):
        results = service.classify_track({"title": "Song", "artists": [{"name": "Queen"}]}, [playlist])

    assert len(results) == 1
    assert results[0].explanation.signal == "description_match"


def test_generate_vibe_description_summarizes_the_tracks():
    genre_lookup = MagicMock(spec=GenreLookupService)
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")
    tracks = [{"videoId": "v1", "title": "Song", "artists": [{"name": "Daft Punk"}]}]

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Upbeat French house and electronic tracks."
    with patch("app.services.classification.completion", return_value=mock_response):
        description = service.generate_vibe_description(tracks)

    assert description == "Upbeat French house and electronic tracks."


def test_generate_vibe_description_is_none_without_an_llm_key_or_tracks():
    genre_lookup = MagicMock(spec=GenreLookupService)
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    bpm_lookup.openrouter_api_key = None
    track = {"videoId": "v1", "title": "t", "artists": [{"name": "a"}]}

    no_key_service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key=None)
    assert no_key_service.generate_vibe_description([track]) is None

    keyed_service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")
    assert keyed_service.generate_vibe_description([]) is None


def test_build_candidate_backfills_a_missing_description_from_real_playlist_content(session):
    from app.models.user import User

    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    playlist_repo = PlaylistRepository(session)
    playlist = playlist_repo.create(
        Playlist(
            user_id=user.id, name="Existing", description=None, rule=None, youtube_playlist_id="yt-1"
        )
    )
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.get_playlist_tracks.return_value = [
        {"videoId": "v1", "title": "Song", "artists": [{"name": "Daft Punk"}]}
    ]
    genre_lookup = MagicMock(spec=GenreLookupService)
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    classification_service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "French house vibes."
    with patch("app.services.classification.completion", return_value=mock_response):
        candidate = build_candidate(music_client, classification_service, playlist_repo, playlist)

    assert candidate.description == "French house vibes."
    assert candidate.artist_counts == {"daftpunk": 1}
    persisted = playlist_repo.get(playlist.id)
    assert persisted.description == "French house vibes."


def test_build_candidate_does_not_overwrite_an_existing_description(session):
    from app.models.user import User

    user = User(display_name="Test User")
    session.add(user)
    session.commit()
    session.refresh(user)
    playlist_repo = PlaylistRepository(session)
    playlist = playlist_repo.create(
        Playlist(
            user_id=user.id,
            name="Existing",
            description="already has a vibe",
            rule=None,
            youtube_playlist_id="yt-1",
        )
    )
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.get_playlist_tracks.return_value = [
        {"videoId": "v1", "title": "Song", "artists": [{"name": "Daft Punk"}]}
    ]
    genre_lookup = MagicMock(spec=GenreLookupService)
    bpm_lookup = MagicMock(spec=BpmLookupService)
    bpm_lookup.lookup_bpm.return_value = BpmLookupResult(bpm=None, source=None)
    classification_service = ClassificationService(genre_lookup, bpm_lookup, openrouter_api_key="or-key")

    with patch("app.services.classification.completion") as mock_completion:
        candidate = build_candidate(music_client, classification_service, playlist_repo, playlist)

    mock_completion.assert_not_called()
    assert candidate.description == "already has a vibe"
