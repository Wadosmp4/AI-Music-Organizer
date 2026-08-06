from unittest.mock import MagicMock, patch

import pytest
import requests

from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.services.bpm_lookup import BpmLookupResult, BpmLookupService
from app.services.classification import CandidatePlaylist, ClassificationService
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

    result = service.classify_track({"title": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]}, [playlist])

    assert isinstance(result.confidence, float)
    assert 0.0 <= result.confidence <= 1.0


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

    result = service.classify_track({"title": "Slow Song", "artists": [{"name": "Someone"}]}, [ruled])

    # The rule (bpm_min=150) rejects this 90bpm song outright; its description
    # ("chill background music") must not be used to route it there anyway (KTD10).
    assert result.playlist_id is None
    assert result.explanation.signal == "none"


def test_explanation_names_the_rule_signal():
    ruled = CandidatePlaylist(id=1, name="Rock Only", rule={"genre": "rock"}, description=None, artist_counts={})
    service = _service(genre="rock")

    result = service.classify_track({"title": "Song", "artists": [{"name": "Band"}]}, [ruled])

    assert result.explanation.signal == "rule"
    assert "Rock Only" in result.explanation.detail


def test_explanation_names_the_artist_similarity_signal():
    playlist = CandidatePlaylist(
        id=1, name="Queen Hits", rule=None, description=None, artist_counts={"queen": 5}
    )
    service = _service()

    result = service.classify_track({"title": "Song", "artists": [{"name": "Queen"}]}, [playlist])

    assert result.explanation.signal == "artist_similarity"
    assert "Queen Hits" in result.explanation.detail


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
        result = service.classify_track({"title": "Song", "artists": [{"name": "Nobody"}]}, [playlist])

    assert result.playlist_id is None
    assert result.explanation.signal == "none"

    # A second, independent call still proceeds normally — one song's failure
    # doesn't leave the service or health store in a state that blocks the next.
    second_result = service.classify_track({"title": "Other Song", "artists": [{"name": "Nobody"}]}, [playlist])
    assert second_result.explanation.signal == "none"


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

    aurora_result = service.classify_track(
        {"title": "Runaway", "artists": [{"name": "AURORA"}]}, [aurora_playlist, aot_playlist]
    )
    aot_result = service.classify_track(
        {"title": "Guren no Yumiya", "artists": [{"name": "Linked Horizon"}]},
        [aurora_playlist, aot_playlist],
    )

    assert aurora_result.playlist_id == 1
    assert aot_result.playlist_id == 2


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
