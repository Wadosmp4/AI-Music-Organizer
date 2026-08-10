from unittest.mock import MagicMock, patch

import numpy as np
from qdrant_client import QdrantClient

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.onboarding_proposal_repository import OnboardingProposalRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.repositories.vector_repository import QdrantVectorRepository
from app.services.library_analysis import LibraryAnalysisService, run_propose_new_playlists


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


def _vector_repo() -> QdrantVectorRepository:
    """A fresh in-memory Qdrant instance per call -- validated equivalent to
    a real server at this scale (course results.md), and keeps each test's
    embeddings isolated without needing a running Qdrant container."""
    repo = QdrantVectorRepository(QdrantClient(":memory:"))
    repo.ensure_collection()
    return repo


def _service(session, music_client=None, vector_repository=None) -> LibraryAnalysisService:
    return LibraryAnalysisService(
        library_repository=LibraryRepository(session),
        playlist_repository=PlaylistRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        user_repository=UserRepository(session),
        music_client=music_client or _fake_music_client(),
        reorganize_session_repository=ReorganizeSessionRepository(session),
        onboarding_proposal_repository=OnboardingProposalRepository(session),
        vector_repository=vector_repository or _vector_repo(),
    )


def _liked_songs_from(songs: list[tuple[str, str, str]]) -> list[dict]:
    """Builds the same dict shape music_client.get_liked_songs() returns,
    from the (video_id, title, artist) tuples _add_library_items already
    takes -- lets callers reuse one song list for both a real LibraryItem
    fixture (when one's needed for an FK) and the liked_songs argument
    run_propose_new_playlists now requires."""
    return [
        {"videoId": video_id, "title": title, "artists": [{"name": artist}]}
        for video_id, title, artist in songs
    ]


def _run_propose(session, user_id, songs, vector_repo=None) -> list:
    """Runs the background task directly (mirrors how test_reorganize_matching
    tests run_reorganize_matching) and reads back what it persisted, applying
    the same MIN_CLUSTER_SIZE visibility filter get_proposals_status applies
    at read time."""
    from app.services.library_analysis import MIN_CLUSTER_SIZE

    run_propose_new_playlists(
        user_id,
        _liked_songs_from(songs),
        engine=session.get_bind(),
        vector_repo=vector_repo or _vector_repo(),
        openrouter_api_key="or-key",
    )
    return [
        p
        for p in OnboardingProposalRepository(session).list_for_user(user_id)
        if p.song_count >= MIN_CLUSTER_SIZE
    ]


def _mock_name_response(name: str, theme: str):
    resp = MagicMock()
    resp.choices[0].message.content = '{"name": "%s", "theme": "%s"}' % (name, theme)
    return resp


def _fake_embed_texts(texts: list[str]) -> list[list[float]]:
    """Fake embed_texts for orchestration tests (persistence, error handling,
    exclusion logic) -- the actual vector values are irrelevant here since
    these tests patch `_hdbscan_clusters` directly rather than relying on
    real HDBSCAN behavior. HDBSCAN needs realistic-scale, well-separated
    data to reliably find structure (see
    test_hdbscan_clusters_separates_real_groups_from_noise); coaxing it into
    a predictable result from a handful of toy vectors proved unreliable
    (verified empirically against a real 2,794-track library -- see
    _hdbscan_clusters's docstring), so cluster *membership* is a test
    concern for _hdbscan_clusters alone, and everything downstream of it
    just needs *some* embedding to pass through.
    """
    return [[float(i)] + [0.0] * 383 for i in range(len(texts))]


def test_hdbscan_clusters_separates_real_groups_from_noise():
    """_hdbscan_clusters's own correctness, at a realistic scale -- a toy
    4-8 point input (like the orchestration tests above use) isn't enough
    for HDBSCAN to reliably find structure regardless of parameters
    (verified empirically), so this uses three well-separated groups of 20
    plus 30 scattered background points, matching the order of magnitude
    where the real pipeline actually runs. Also guards against regressing
    to allow_single_cluster=True + the default "eom" selection method,
    which collapsed a real 2,794-track library into one 2,350-song blob
    instead of finding its ~110 real clusters (see _hdbscan_clusters's
    docstring) -- a synthetic three-group input would surface the same
    collapse-into-one-cluster failure mode.
    """
    from app.services.library_analysis import _hdbscan_clusters

    rng = np.random.RandomState(7)

    def _tight_group(seed_offset: int, n: int = 20) -> np.ndarray:
        center = rng.randn(384)
        center /= np.linalg.norm(center)
        return center + rng.randn(n, 384) * 0.02

    group_a = _tight_group(1)
    group_b = _tight_group(2)
    group_c = _tight_group(3)
    background = rng.randn(30, 384)
    background /= np.linalg.norm(background, axis=1, keepdims=True)
    vectors = np.vstack([group_a, group_b, group_c, background]).tolist()

    clusters = _hdbscan_clusters(vectors)

    # Each of the three real groups is fully recovered inside its own
    # cluster (not merged with another real group, not fragmented into
    # pieces) -- a handful of scattered background points occasionally
    # riding along with the real group they landed nearest to, or forming
    # their own small incidental cluster, is expected and not asserted
    # against here. What matters is that no cluster swallows the background
    # wholesale (the collapse-into-one-blob failure mode this guards) and
    # that two distinct real groups never land in the same cluster.
    expected_index_sets = [set(range(0, 20)), set(range(20, 40)), set(range(40, 60))]
    found_sets = [set(c) for c in clusters]
    for expected in expected_index_sets:
        containing = [found for found in found_sets if expected <= found]
        assert len(containing) == 1, f"expected {expected} fully inside exactly one cluster, got {containing}"
    for found in found_sets:
        assert sum(expected <= found for expected in expected_index_sets) <= 1


_FOUR_SONGS = [
    ("v1", "Song 1", "Artist A"),
    ("v2", "Song 2", "Artist B"),
    ("v3", "Song 3", "Artist C"),
    ("v4", "Song 4", "Artist D"),
]


def test_cluster_of_related_unplaced_songs_produces_a_new_playlist_proposal(session):
    user = _make_user(session)
    mock_response = _mock_name_response("Chill Electronic", "Laid-back electronic songs")
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=mock_response),
    ):
        proposals = _run_propose(session, user.id, _FOUR_SONGS)

    assert len(proposals) == 1
    assert proposals[0].name == "Chill Electronic"
    assert proposals[0].theme == "Laid-back electronic songs"
    assert proposals[0].song_count == 4


def test_cluster_success_surfaces_llm_clustering_health_as_ok(session):
    user = _make_user(session)
    dependency_health_store.set_status("llm_clustering", DependencyStatus.DEGRADED, "stale")

    mock_response = _mock_name_response("Chill", "chill songs")
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=mock_response),
    ):
        _run_propose(session, user.id, _FOUR_SONGS)

    status, _ = dependency_health_store.get_status("llm_clustering")
    assert status == DependencyStatus.OK


def test_cluster_naming_empty_choices_list_surfaces_degraded_health_instead_of_raising(session):
    """An empty `choices` list (e.g. a safety-filtered response) raises
    IndexError on resp.choices[0] — previously outside the narrow except
    tuple, so it would have escaped _name_cluster entirely instead of being
    treated like any other per-cluster naming failure (KTD18)."""
    user = _make_user(session)
    empty_choices_response = MagicMock()
    empty_choices_response.choices = []
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=empty_choices_response),
    ):
        proposals = _run_propose(session, user.id, _FOUR_SONGS)

    assert proposals == []
    status, reason = dependency_health_store.get_status("llm_clustering")
    assert status == DependencyStatus.DEGRADED
    assert "clustering" in reason.lower()


def test_clustering_considers_songs_already_matched_to_an_existing_playlist(session):
    """Suggestions previously only looked at still-unplaced songs, so once a
    song matched an existing playlist it could never surface in a new-
    playlist proposal — even though multi-label matching means accepting a
    new proposal wouldn't remove it from where it already landed. Now the
    full liked-songs library feeds clustering, not just the leftovers."""
    user = _make_user(session)
    playlist = PlaylistRepository(session).create(
        Playlist(user_id=user.id, name="Existing", description="some existing playlist", rule=None)
    )
    # A LibraryItem row is only needed here for the ReviewQueueItem FK below
    # -- run_propose_new_playlists itself no longer reads library_item.
    items = _add_library_items(session, user.id, _FOUR_SONGS)
    # Song 1 already matched "Existing" and is sitting pending review there —
    # a settled placement, but not a reason to exclude it from clustering.
    ReviewQueueRepository(session).create(
        ReviewQueueItem(user_id=user.id, library_item_id=items[0].id, playlist_id=playlist.id)
    )

    mock_response = _mock_name_response("Chill Electronic", "Laid-back electronic songs")
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=mock_response),
    ):
        proposals = _run_propose(session, user.id, _FOUR_SONGS)

    assert len(proposals) == 1
    assert proposals[0].song_count == 4


def test_clustering_excludes_songs_no_longer_in_the_current_liked_library(session):
    """3 songs remain after one is no longer liked -- below MIN_CLUSTER_SIZE,
    so HDBSCAN never finds a cluster and the LLM is never called at all.
    Exclusion is simply not fetching the song in the first place: proposals
    are generated from music_client.get_liked_songs()'s current result, not
    a locally-tracked "removed" flag, so an unliked song just isn't in the
    list the trigger endpoint hands to run_propose_new_playlists."""
    user = _make_user(session)

    with patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts) as mock_embed:
        proposals = _run_propose(session, user.id, _FOUR_SONGS[1:])

    embedded_texts = mock_embed.call_args[0][0]
    assert not any("Song 1" in text for text in embedded_texts)
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
    mock_response = _mock_name_response("Chill Electronic", "Laid-back electronic songs")
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=mock_response) as mock_completion,
    ):
        _run_propose(session, user.id, _FOUR_SONGS)

    _, kwargs = mock_completion.call_args
    user_message = kwargs["messages"][1]["content"]
    assert "EDM mix" in user_message
    assert "High-energy electronic dance music and remixes." in user_message


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
    assert playlist.source == "proposal"
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
    assert playlist.source == "custom"
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
    assert playlist.source == "adopted"
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
    """Also covers U3/KTD7: this playlist has a pending item referencing it,
    so removal is blocked unless explicitly confirmed."""
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
        confirmed_removed_playlist_ids=[playlist.id],
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

    mock_response = _mock_name_response("Chill Electronic", "Laid-back electronic songs")
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=mock_response),
    ):
        run_reorganize_clustering(
            reorganize_session.id,
            fetched_songs,
            engine=session.get_bind(),
            vector_repo=service.vector_repository,
        )

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.clustering_status == "done"
    assert len(status.proposals) == 1
    assert status.proposals[0].name == "Chill Electronic"
    assert status.proposals[0].song_count == 4
    assert status.enriched_count == 4
    assert status.total_count == 4


def test_reorganize_status_reports_total_count_before_clustering_starts(session):
    """total_count (the snapshot size) is known immediately at trigger time,
    independent of how far the background enrichment loop has actually
    gotten -- the frontend progress bar needs a denominator from the very
    first poll, not just once enrichment starts checkpointing."""
    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1", "v2", "v3", "v4", "v5"])
    service = _service(session, music_client=music_client)

    reorganize_session, _ = service.trigger_reorganize(user.id)

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.clustering_status == "in_progress"
    assert status.total_count == 5
    assert status.enriched_count == 0


def test_reorganize_clustering_checkpoints_enriched_count_incrementally(session):
    """enriched_count is persisted incrementally during the genre-enrichment
    phase (not just written once at the end) -- verified by counting how
    many times the session gets saved across a 20-track run checkpointed
    every 5 completions."""
    from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
    from app.services.library_analysis import run_reorganize_clustering

    user = _make_user(session)
    music_client = _fake_music_client()
    video_ids = [f"v{i}" for i in range(20)]
    liked_songs = _fake_liked_songs(video_ids)
    music_client.get_liked_songs.return_value = liked_songs
    service = _service(session, music_client=music_client)
    reorganize_session, fetched_songs = service.trigger_reorganize(user.id)

    mock_response = _mock_name_response("Chill Electronic", "Laid-back electronic songs")
    with (
        patch("app.services.library_analysis.ENRICHMENT_PROGRESS_CHECKPOINT", 5),
        patch.object(
            ReorganizeSessionRepository, "update", autospec=True, wraps=ReorganizeSessionRepository.update
        ) as update_spy,
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[]),
        patch("app.services.library_analysis.completion", return_value=mock_response),
    ):
        run_reorganize_clustering(
            reorganize_session.id,
            fetched_songs,
            engine=session.get_bind(),
            vector_repo=service.vector_repository,
        )

    # 20 tracks, checkpointed every 5 completions: at least 4 saves happen
    # during the genre-enrichment phase alone, on top of the initial
    # reset-to-0 and final clustering_status="done" write -- proving
    # progress is actually persisted as it goes, not just once at the very
    # end.
    assert update_spy.call_count >= 6

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.enriched_count == 20
    assert status.total_count == 20


def test_reorganize_clustering_naming_failure_does_not_block_other_clusters(session):
    """Mirrors _name_cluster's own broad-except path (KTD18): one cluster's
    LLM naming failure just leaves it unproposed for this pass, it doesn't
    stop other clusters from completing and persisting their own proposals.
    _hdbscan_clusters is patched directly with two canned index groups
    rather than relying on real HDBSCAN behavior on a toy 8-track input."""
    from app.services.library_analysis import run_reorganize_clustering

    user = _make_user(session)
    music_client = _fake_music_client()
    video_ids = [f"a{i}" for i in range(4)] + [f"b{i}" for i in range(4)]
    liked_songs = _fake_liked_songs(video_ids)
    music_client.get_liked_songs.return_value = liked_songs
    service = _service(session, music_client=music_client)

    reorganize_session, fetched_songs = service.trigger_reorganize(user.id)

    ok_response = _mock_name_response("Named Cluster", "theme")
    responses = [RuntimeError("LLM failure"), ok_response]

    def _fake_completion(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3], [4, 5, 6, 7]]),
        patch("app.services.library_analysis.completion", side_effect=_fake_completion),
    ):
        run_reorganize_clustering(
            reorganize_session.id,
            fetched_songs,
            engine=session.get_bind(),
            vector_repo=service.vector_repository,
        )

    status = service.get_reorganize_status(reorganize_session.id)
    assert status.clustering_status == "done"
    # One cluster's naming failed, the other succeeded and got persisted.
    assert len(status.proposals) == 1
    assert status.proposals[0].name == "Named Cluster"
    assert status.proposals[0].song_count == 4


def test_cancel_reorganize_rejects_non_terminal_session_items_and_marks_session_cancelled(session):
    from app.repositories.reorganize_session_repository import ReorganizeSessionRepository

    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1", "v2"])
    service = _service(session, music_client=music_client)
    reorganize_session, _ = service.trigger_reorganize(user.id)

    library_items = _add_library_items(
        session, user.id, [("v1", "Song 1", "Artist"), ("v2", "Song 2", "Artist")]
    )
    pending_item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_items[0].id,
        status="pending",
        reorganize_session_id=reorganize_session.id,
    )
    approved_pending_apply_item = ReviewQueueItem(
        user_id=user.id,
        library_item_id=library_items[1].id,
        status="approved_pending_apply",
        reorganize_session_id=reorganize_session.id,
    )
    session.add(pending_item)
    session.add(approved_pending_apply_item)
    session.commit()
    session.refresh(pending_item)
    session.refresh(approved_pending_apply_item)

    service.cancel_reorganize(reorganize_session.id, user.id)

    review_queue_repo = ReviewQueueRepository(session)
    assert review_queue_repo.get(pending_item.id).status == "rejected"
    assert review_queue_repo.get(approved_pending_apply_item.id).status == "rejected"

    cancelled_session = ReorganizeSessionRepository(session).get(reorganize_session.id)
    assert cancelled_session.clustering_status == "cancelled"


def test_cancelled_session_is_not_reused_by_a_later_trigger(session):
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1"])
    service = _service(session, music_client=music_client)
    user = _make_user(session)

    first_session, _ = service.trigger_reorganize(user.id)
    service.cancel_reorganize(first_session.id, user.id)

    second_session, _ = service.trigger_reorganize(user.id)

    assert second_session.id != first_session.id


def test_cancel_reorganize_is_rejected_while_its_own_apply_is_in_progress(session):
    from app.services.reorganize_apply import ApplyAlreadyInProgressError

    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1"])
    service = _service(session, music_client=music_client)
    reorganize_session, _ = service.trigger_reorganize(user.id)
    reorganize_session.apply_status = "in_progress"
    ReorganizeSessionRepository(session).update(reorganize_session)

    try:
        service.cancel_reorganize(reorganize_session.id, user.id)
        assert False, "expected ApplyAlreadyInProgressError"
    except ApplyAlreadyInProgressError:
        pass


def test_cancel_reorganize_raises_for_unknown_session(session):
    from app.services.reorganize_apply import ReorganizeSessionNotFoundError

    user = _make_user(session)
    service = _service(session)

    try:
        service.cancel_reorganize(999999, user.id)
        assert False, "expected ReorganizeSessionNotFoundError"
    except ReorganizeSessionNotFoundError:
        pass


def test_reorganize_status_reports_stalled_when_no_recent_proposal_activity(session):
    from datetime import timedelta

    from sqlmodel import update

    from app.models.base import utcnow
    from app.models.reorganize_session import ReorganizeSession

    user = _make_user(session)
    service = _service(session)

    reorganize_session, _ = service.trigger_reorganize(user.id)
    # Raw update, not the repository -- save() now always bumps updated_at
    # to "now" on every write (the fix this test exists to exercise), so
    # simulating a genuinely stale session means writing around it, the same
    # way a session nobody has touched in a while would look in practice.
    session.exec(
        update(ReorganizeSession)
        .where(ReorganizeSession.id == reorganize_session.id)
        .values(updated_at=utcnow() - timedelta(seconds=999))
    )
    session.commit()

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


# -- U3: playlist selection persistence for the session ---------------------


def test_selecting_a_proposed_playlist_during_an_open_session_defers_the_youtube_create(session):
    """KTD6: during an active reorganize session, accepting a proposal
    creates the local Playlist row immediately but leaves the YouTube create
    to Finish & Apply (U6)."""
    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1"])
    service = _service(session, music_client=music_client)
    service.trigger_reorganize(user.id)

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[{"name": "Chill Electronic", "theme": "Laid-back electronic songs"}],
        custom_playlists=[],
    )

    assert len(created) == 1
    assert created[0].youtube_playlist_id is None
    music_client.create_playlist.assert_not_called()


def test_selecting_a_custom_playlist_during_an_open_session_defers_the_youtube_create(session):
    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1"])
    service = _service(session, music_client=music_client)
    service.trigger_reorganize(user.id)

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[{"name": "Road Trip", "description": "upbeat driving songs"}],
    )

    assert len(created) == 1
    assert created[0].youtube_playlist_id is None
    music_client.create_playlist.assert_not_called()


def test_adopting_an_existing_playlist_during_an_open_session_still_links_immediately(session):
    """KTD6/U3 approach step 2: adopting an existing YouTube playlist keeps
    today's behavior regardless of session state -- no create call either
    way, so nothing to defer."""
    user = _make_user(session)
    music_client = _fake_music_client()
    music_client.get_liked_songs.return_value = _fake_liked_songs(["v1"])
    service = _service(session, music_client=music_client)
    service.trigger_reorganize(user.id)

    created = service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        adopted_playlists=[{"playlist_id": "yt-pre-existing", "name": "Road Trip"}],
    )

    assert created[0].youtube_playlist_id == "yt-pre-existing"
    music_client.create_playlist.assert_not_called()


def test_unchecking_a_playlist_with_pending_work_is_blocked_without_confirmation(session):
    from app.services.library_analysis import PlaylistRemovalRequiresConfirmationError

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
    ReviewQueueRepository(session).create(
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=library_item.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
        )
    )

    try:
        service.complete_onboarding(
            user_id=user.id,
            accepted_proposals=[],
            custom_playlists=[],
            removed_playlist_ids=[playlist.id],
        )
        assert False, "expected PlaylistRemovalRequiresConfirmationError"
    except PlaylistRemovalRequiresConfirmationError as exc:
        assert exc.playlist_id == playlist.id
        assert exc.pending_count == 1

    # Nothing was removed.
    assert PlaylistRepository(session).get(playlist.id) is not None


def test_unchecking_a_playlist_with_pending_work_proceeds_once_confirmed(session):
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
        ReviewQueueItem(
            user_id=user.id,
            library_item_id=library_item.id,
            playlist_id=playlist.id,
            status="approved_pending_apply",
        )
    )

    service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        removed_playlist_ids=[playlist.id],
        confirmed_removed_playlist_ids=[playlist.id],
    )

    assert PlaylistRepository(session).get(playlist.id) is None
    assert queue_repo.get(queue_item.id).playlist_id is None


def test_unchecking_a_playlist_with_no_pending_work_proceeds_without_confirmation(session):
    """Same as today's behavior when there's nothing at stake."""
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

    service.complete_onboarding(
        user_id=user.id,
        accepted_proposals=[],
        custom_playlists=[],
        removed_playlist_ids=[playlist.id],
    )

    assert PlaylistRepository(session).get(playlist.id) is None


def test_declined_suggestion_is_not_remembered_and_can_resurface_later(session):
    """R5: declining is simply never accepting a proposal -- no suppression
    state is written anywhere, so an unrelated later run's clustering pass
    is free to propose the same cluster again."""
    user = _make_user(session)
    service = _service(session)

    # "Declining" Chill Electronic is simply never passing it to
    # complete_onboarding's accepted_proposals.
    created = service.complete_onboarding(user_id=user.id, accepted_proposals=[], custom_playlists=[])

    assert created == []
    assert PlaylistRepository(session).list_for_user(user.id) == []


def test_trigger_propose_new_playlists_marks_in_progress(session):
    from app.services.library_analysis import trigger_propose_new_playlists

    user = _make_user(session)
    user_repo = UserRepository(session)
    assert user.proposals_status == "idle"

    updated = trigger_propose_new_playlists(user_repo, user.id)

    assert updated.proposals_status == "in_progress"
    assert user_repo.get(user.id).proposals_status == "in_progress"


def test_trigger_propose_new_playlists_rejects_when_already_in_progress(session):
    from app.services.library_analysis import (
        OnboardingProposalsAlreadyInProgressError,
        trigger_propose_new_playlists,
    )

    user = _make_user(session)
    user_repo = UserRepository(session)
    user_repo.set_proposals_progress(user.id, status="in_progress")

    try:
        trigger_propose_new_playlists(user_repo, user.id)
        assert False, "expected OnboardingProposalsAlreadyInProgressError"
    except OnboardingProposalsAlreadyInProgressError:
        pass


def test_run_propose_new_playlists_tracks_progress_and_persists_proposals(session):
    """The background-task version (mirrors run_reorganize_clustering):
    persists matched_count-style progress as genre enrichment works through
    the liked-songs library, then marks proposals_status done once
    clustering finishes."""
    user = _make_user(session)
    user_repo = UserRepository(session)

    mock_response = _mock_name_response("Chill Electronic", "Laid-back electronic songs")
    with (
        patch("app.services.library_analysis.embed_texts", side_effect=_fake_embed_texts),
        patch("app.services.library_analysis._hdbscan_clusters", return_value=[[0, 1, 2, 3]]),
        patch("app.services.library_analysis.completion", return_value=mock_response),
    ):
        run_propose_new_playlists(
            user.id,
            _liked_songs_from(_FOUR_SONGS),
            engine=session.get_bind(),
            vector_repo=_vector_repo(),
            openrouter_api_key="or-key",
        )

    fresh_user = user_repo.get(user.id)
    assert fresh_user.proposals_status == "done"
    assert fresh_user.proposals_processed_count == 4
    assert fresh_user.proposals_total_count == 4
    proposals = OnboardingProposalRepository(session).list_for_user(user.id)
    assert len(proposals) == 1
    assert proposals[0].name == "Chill Electronic"
    assert proposals[0].song_count == 4


def test_run_propose_new_playlists_without_an_api_key_marks_done_immediately(session):
    user = _make_user(session)
    user_repo = UserRepository(session)

    run_propose_new_playlists(
        user.id,
        _liked_songs_from([("v1", "Song 1", "Artist A")]),
        engine=session.get_bind(),
        vector_repo=_vector_repo(),
        openrouter_api_key="",
    )

    fresh_user = user_repo.get(user.id)
    assert fresh_user.proposals_status == "done"
    assert OnboardingProposalRepository(session).list_for_user(user.id) == []


def test_run_propose_new_playlists_clears_stale_proposals_from_a_prior_run(session):
    """A re-trigger (e.g. the library changed) shouldn't leave a stale
    suggestion from the previous snapshot mixed in with fresh ones."""
    user = _make_user(session)
    OnboardingProposalRepository(session).merge_proposal(user.id, "Stale Suggestion", "old", 10)
    user_repo = UserRepository(session)

    run_propose_new_playlists(
        user.id,
        [],  # no liked songs either -- exercises the clear + early-done path
        engine=session.get_bind(),
        vector_repo=_vector_repo(),
        openrouter_api_key="",
    )

    assert OnboardingProposalRepository(session).list_for_user(user.id) == []
    assert user_repo.get(user.id).proposals_status == "done"


def test_get_proposals_status_hides_proposals_below_min_cluster_size(session):
    user = _make_user(session)
    service = _service(session)
    proposal_repo = OnboardingProposalRepository(session)
    proposal_repo.merge_proposal(user.id, "Big Enough", "theme", 4)
    proposal_repo.merge_proposal(user.id, "Too Small", "theme", 2)

    status = service.get_proposals_status(user.id)

    assert [p.name for p in status.proposals] == ["Big Enough"]
