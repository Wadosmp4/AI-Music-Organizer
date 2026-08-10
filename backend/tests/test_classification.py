from unittest.mock import MagicMock, patch

import pytest

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.playlist import Playlist
from app.repositories.playlist_repository import PlaylistRepository
from app.services.classification import (
    CandidatePlaylist,
    ClassificationResult,
    ClassificationService,
    Explanation,
    build_artist_counts,
    build_candidate,
    classify_tracks_concurrently,
)
from app.services.genre_lookup import GenreLookupService


@pytest.fixture(autouse=True)
def reset_dependency_health():
    dependency_health_store.set_status("lastfm", DependencyStatus.OK)
    dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)
    dependency_health_store.set_status("llm_clustering", DependencyStatus.OK)
    yield


def _service(genre=None):
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = genre
    # Explicit "" (not the default None): None now falls through to
    # get_settings().openrouter_api_key, which could pick up a real key from
    # the process environment and make a real, billed API call during a
    # test that never mocks completion().
    return ClassificationService(genre_lookup, openrouter_api_key="")


def test_every_suggestion_carries_a_confidence_score():
    playlist = CandidatePlaylist(id=1, name="Rock", rule=None, description=None, artist_counts={"queen": 5})
    service = _service()

    results = service.classify_track({"title": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]}, [playlist])

    assert len(results) == 1
    assert isinstance(results[0].confidence, float)
    assert 0.0 <= results[0].confidence <= 1.0


def test_rule_gated_playlist_ignores_description_only_match():
    ruled = CandidatePlaylist(
        id=1, name="Electronic Only", rule={"genre": "electronic"}, description="chill background music", artist_counts={}
    )
    service = _service(genre="rock")

    results = service.classify_track({"title": "Slow Song", "artists": [{"name": "Someone"}]}, [ruled])

    # The rule (genre=electronic) rejects this rock song outright; its
    # description ("chill background music") must not be used to route it
    # there anyway (KTD10).
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
    service = ClassificationService(genre_lookup, openrouter_api_key="or-key")

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


def test_classify_tracks_concurrently_without_an_engine_uses_the_given_service_in_order():
    """Default (engine=None, every pre-existing call site): a plain
    sequential loop against the exact classification_service object given --
    proves a mocked service's configured return_value still drives the
    result, not a reconstructed one."""
    tracks = [{"videoId": f"v{i}", "title": f"Song {i}"} for i in range(3)]
    service = MagicMock(spec=ClassificationService)
    service.classify_track.side_effect = lambda track, candidates, user_id: [
        ClassificationResult(
            playlist_id=1, confidence=0.9, explanation=Explanation("rule", track["videoId"]), genre=None
        )
    ]

    results = classify_tracks_concurrently(tracks, [], service, user_id=None)

    assert [r[0].explanation.detail for r in results] == ["v0", "v1", "v2"]


def test_classify_tracks_concurrently_without_an_engine_isolates_one_failure():
    tracks = [{"videoId": "v0"}, {"videoId": "v1"}, {"videoId": "v2"}]
    service = MagicMock(spec=ClassificationService)

    def _classify(track, candidates, user_id):
        if track["videoId"] == "v1":
            raise RuntimeError("boom")
        return [ClassificationResult(playlist_id=1, confidence=0.5, explanation=Explanation("rule", "x"), genre=None)]

    service.classify_track.side_effect = _classify

    results = classify_tracks_concurrently(tracks, [], service, user_id=None)

    assert results[0] is not None
    assert results[1] is None  # KTD18: this track's failure doesn't raise or drop the others
    assert results[2] is not None


def test_classify_tracks_concurrently_with_an_engine_classifies_every_track_independently(engine):
    """The concurrent path (engine given): each worker builds its own
    ClassificationService bound to its own DB session rather than sharing
    the caller's -- verified end to end with a real ClassificationService
    (openrouter_api_key="" so no real LLM call is possible) against the
    real per-track artist-similarity signal, across enough tracks to
    exercise the thread pool.

    Patches get_settings so every worker's independently-constructed
    GenreLookupService sees lastfm_api_key="" regardless of what's actually
    configured in this environment -- each worker builds its own
    GenreLookupService straight from Settings (see
    classify_tracks_concurrently), not from anything this test can pass in
    directly, so this is the only way to keep the test's genre lookups from
    making a real, non-deterministic Last.fm call. (The concurrent
    genre-cache-write race this surfaced during development is covered
    directly and deterministically by
    test_genre_cache_upsert_recovers_from_a_concurrent_insert_race below,
    without needing real threads or a real Last.fm key.)
    """
    playlist = CandidatePlaylist(
        id=7, name="Rock", rule=None, description=None, artist_counts={"queen": 5, "abba": 5}
    )
    genre_lookup = MagicMock(spec=GenreLookupService)
    genre_lookup.genre_for.return_value = None
    service = ClassificationService(genre_lookup, openrouter_api_key="")

    tracks = [
        {"title": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]},
        {"title": "Dancing Queen", "artists": [{"name": "ABBA"}]},
        {"title": "Unknown Song", "artists": [{"name": "Some Random Artist"}]},
    ] * 4  # 12 tracks, comfortably more than CLASSIFY_MAX_WORKERS

    fake_settings = MagicMock(lastfm_api_key="")
    with patch("app.services.genre_lookup.get_settings", return_value=fake_settings):
        results = classify_tracks_concurrently(tracks, [playlist], service, user_id=None, engine=engine)

    assert len(results) == len(tracks)
    for track, track_results in zip(tracks, results):
        assert track_results is not None
        artist = track["artists"][0]["name"].lower()
        if artist in {"queen", "abba"}:
            assert track_results[0].playlist_id == 7
            assert track_results[0].explanation.signal == "artist_similarity"
        else:
            assert track_results[0].playlist_id is None


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
    """Raised by call_with_retry's circuit_breaker.before_call(), not by the
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
    service = ClassificationService(genre_lookup, openrouter_api_key="or-key")

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
    service = ClassificationService(genre_lookup, openrouter_api_key="or-key")

    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"matched_playlists": ["Arena Rock Anthems"]}'
    with patch("app.services.classification.completion", return_value=mock_response):
        results = service.classify_track({"title": "Song", "artists": [{"name": "Queen"}]}, [playlist])

    assert len(results) == 1
    assert results[0].explanation.signal == "description_match"


def test_generate_vibe_description_summarizes_the_tracks():
    genre_lookup = MagicMock(spec=GenreLookupService)
    service = ClassificationService(genre_lookup, openrouter_api_key="or-key")
    tracks = [{"videoId": "v1", "title": "Song", "artists": [{"name": "Daft Punk"}]}]

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Upbeat French house and electronic tracks."
    with patch("app.services.classification.completion", return_value=mock_response):
        description = service.generate_vibe_description(tracks)

    assert description == "Upbeat French house and electronic tracks."


def test_generate_vibe_description_is_none_without_an_llm_key_or_tracks():
    genre_lookup = MagicMock(spec=GenreLookupService)
    track = {"videoId": "v1", "title": "t", "artists": [{"name": "a"}]}

    # Explicit "" (not None): None now falls through to
    # get_settings().openrouter_api_key, which could pick up a real key from
    # the process environment and make a real, billed API call here.
    no_key_service = ClassificationService(genre_lookup, openrouter_api_key="")
    assert no_key_service.generate_vibe_description([track]) is None

    keyed_service = ClassificationService(genre_lookup, openrouter_api_key="or-key")
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
    classification_service = ClassificationService(genre_lookup, openrouter_api_key="or-key")

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
    classification_service = ClassificationService(genre_lookup, openrouter_api_key="or-key")

    with patch("app.services.classification.completion") as mock_completion:
        candidate = build_candidate(music_client, classification_service, playlist_repo, playlist)

    mock_completion.assert_not_called()
    assert candidate.description == "already has a vibe"


def test_genre_cache_upsert_recovers_from_a_concurrent_insert_race(engine):
    """classify_tracks_concurrently's worker threads each open their own DB
    session, so two workers classifying songs by the same not-yet-cached
    artist can both miss GenreCacheRepository's read and race to insert the
    same artist -- reproduced directly and deterministically here (no real
    threads or network calls needed) by simulating the interleaving two
    separate sessions/repositories against the same underlying artist row.
    Before this fix, the loser's IntegrityError propagated out of
    classify_track and silently dropped that song's whole classification
    result (KTD18's broad except treats any failure as "retry next batch").
    """
    from sqlmodel import Session

    from app.repositories.genre_cache_repository import GenreCacheRepository

    with Session(engine) as session_a, Session(engine) as session_b:
        repo_a = GenreCacheRepository(session_a)
        repo_b = GenreCacheRepository(session_b)

        # Both "workers" miss the cache before either has written anything.
        assert repo_a.get("Queen") is None
        assert repo_b.get("Queen") is None

        # The first writer commits its insert...
        repo_a.upsert("Queen", "classic rock")

        # ...and the second, having already missed the cache, must recover
        # from the resulting UNIQUE constraint violation instead of raising.
        recovered = repo_b.upsert("Queen", "classic rock")

    assert recovered.artist == "Queen"
    assert recovered.genre == "classic rock"
    with Session(engine) as verify_session:
        assert GenreCacheRepository(verify_session).get("Queen") is not None
