"""End-to-end HTTP-layer coverage for every router (review-queue, ingestion,
onboarding, playlists) via TestClient — previously entirely absent (the #3
finding this closes): every test elsewhere in the suite calls services
directly, so the domain-exception-to-HTTP-status mapping in app/main.py
(ItemNotFoundError -> 404, StaleItemError/VersionConflictError -> 409) and
each router's own except-Exception -> 502 fallback were completely
unverified end-to-end.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_music_client, get_session
from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import MusicServiceClient
from app.main import app
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.models.user import User
from app.repositories.user_repository import UserRepository

# The backend's CSRF guard (app/main.py) requires this on every mutating
# request — a plain TestClient POST with no headers is indistinguishable
# from a cross-site request otherwise.
_CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture()
def fake_music_client() -> MagicMock:
    music_client = MagicMock(spec=MusicServiceClient)
    music_client.create_playlist.return_value = "yt-playlist-fake-id"
    music_client.get_library_playlists.return_value = []
    return music_client


@pytest.fixture()
def api_client(session, fake_music_client):
    def _get_session_override():
        yield session

    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[get_music_client] = lambda: fake_music_client
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_auth_status():
    auth_status_store.set_write_status(AuthStatus.OK)
    yield
    auth_status_store.set_write_status(AuthStatus.OK)


def _seed_default_user(session) -> User:
    """The default-user id (DEFAULT_USER_ID=1, KTD2) that get_default_user
    auto-creates on first use — created explicitly here so tests can attach
    playlists/library items to it before the first request."""
    from app.api.deps import DEFAULT_USER_ID

    user = User(id=DEFAULT_USER_ID, display_name="Default User")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# review-queue router
# ---------------------------------------------------------------------------


def _seed_review_queue_item(session, user, youtube_playlist_id="PL123"):
    playlist = Playlist(user_id=user.id, name="Rock", youtube_playlist_id=youtube_playlist_id)
    session.add(playlist)
    session.commit()
    session.refresh(playlist)

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
    return playlist, library_item, queue_item


def test_approve_via_http_succeeds_and_calls_music_client(session, api_client, fake_music_client):
    user = _seed_default_user(session)
    _, library_item, queue_item = _seed_review_queue_item(session, user)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/approve",
        json={"expected_version": 1},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    # The song a reviewer just acted on -- previously absent from every
    # review-queue response, making the queue impossible to actually use.
    assert body["title"] == "Song"
    assert body["artist"] == "Artist"
    fake_music_client.add_playlist_items.assert_called_once_with("PL123", [library_item.video_id])


def test_approve_via_http_missing_youtube_link_maps_to_404(session, api_client):
    user = _seed_default_user(session)
    _, _, queue_item = _seed_review_queue_item(session, user, youtube_playlist_id=None)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/approve",
        json={"expected_version": 1},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 404


def test_approve_via_http_version_conflict_maps_to_409(session, api_client):
    user = _seed_default_user(session)
    # Wrong expected_version on a real row (version starts at 1).
    _, _, queue_item = _seed_review_queue_item(session, user)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/approve",
        json={"expected_version": 99},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 409


def test_approve_via_http_generic_write_failure_maps_to_502(
    session, api_client, fake_music_client
):
    user = _seed_default_user(session)
    _, _, queue_item = _seed_review_queue_item(session, user)
    fake_music_client.add_playlist_items.side_effect = RuntimeError("network error")

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/approve",
        json={"expected_version": 1},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 502


def test_move_via_http_stale_item_maps_to_409(session, api_client):
    from datetime import datetime, timezone

    user = _seed_default_user(session)
    other_playlist = Playlist(user_id=user.id, name="Chill", youtube_playlist_id="PL456")
    session.add(other_playlist)
    session.commit()
    session.refresh(other_playlist)
    _, library_item, queue_item = _seed_review_queue_item(session, user)
    library_item.removed_at = datetime.now(timezone.utc)
    session.add(library_item)
    session.commit()

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/move",
        json={"expected_version": 1, "new_playlist_id": other_playlist.id},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 409


def test_reject_via_http_succeeds(session, api_client, fake_music_client):
    user = _seed_default_user(session)
    _, _, queue_item = _seed_review_queue_item(session, user)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/reject",
        json={"expected_version": 1},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    fake_music_client.add_playlist_items.assert_not_called()


def test_reject_via_http_version_conflict_maps_to_409(session, api_client):
    user = _seed_default_user(session)
    _, _, queue_item = _seed_review_queue_item(session, user)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/reject",
        json={"expected_version": 99},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 409


def test_list_review_queue_via_http(session, api_client):
    user = _seed_default_user(session)
    _seed_review_queue_item(session, user)

    response = api_client.get("/api/v1/review-queue")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["title"] == "Song"
    assert body[0]["artist"] == "Artist"


# ---------------------------------------------------------------------------
# ingestion router
# ---------------------------------------------------------------------------


def test_ingestion_check_via_http_is_gated_until_onboarding_completes(
    session, api_client, fake_music_client
):
    _seed_default_user(session)

    response = api_client.post("/api/v1/ingestion/check", headers=_CSRF_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["ran"] is False
    assert body["mode"] == "waiting_for_onboarding"
    fake_music_client.get_liked_songs.assert_not_called()


def test_ingestion_check_via_http_reports_no_new_songs(session, api_client, fake_music_client):
    user = _seed_default_user(session)
    UserRepository(session).mark_onboarding_completed(user.id)
    fake_music_client.get_liked_songs.return_value = []

    response = api_client.post("/api/v1/ingestion/check", headers=_CSRF_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["ran"] is True
    assert body["new_songs_found"] == 0


# ---------------------------------------------------------------------------
# onboarding router
# ---------------------------------------------------------------------------


def test_onboarding_analysis_via_http_lists_added_playlists(session, api_client):
    user = _seed_default_user(session)
    playlist = Playlist(user_id=user.id, name="Existing", description="already here")
    session.add(playlist)
    session.commit()

    response = api_client.get("/api/v1/onboarding/analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["proposals"] == []  # no openrouter_api_key configured in tests
    assert len(body["added_playlists"]) == 1
    assert body["added_playlists"][0]["name"] == "Existing"


def test_onboarding_analysis_via_http_lists_youtube_playlists_not_yet_added(
    session, api_client, fake_music_client
):
    user = _seed_default_user(session)
    session.add(
        Playlist(
            user_id=user.id, name="Already Added", youtube_playlist_id="yt-already-added"
        )
    )
    session.commit()
    fake_music_client.get_library_playlists.return_value = [
        {"playlistId": "yt-already-added", "title": "Already Added"},
        {"playlistId": "yt-pre-existing", "title": "Road Trip"},
    ]

    response = api_client.get("/api/v1/onboarding/analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["existing_playlists"] == [{"playlist_id": "yt-pre-existing", "title": "Road Trip"}]


def test_onboarding_select_via_http_creates_playlist_via_music_client(
    session, api_client, fake_music_client
):
    _seed_default_user(session)

    response = api_client.post(
        "/api/v1/onboarding/select",
        json={
            "accepted_proposals": [{"name": "Chill Electronic", "theme": "Laid-back"}],
            "custom_playlists": [],
        },
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["created_playlists"]) == 1
    assert body["created_playlists"][0]["name"] == "Chill Electronic"
    # #19: the response must not claim a `rule` field the backend never sends.
    assert "rule" not in body["created_playlists"][0]
    fake_music_client.create_playlist.assert_called_once_with("Chill Electronic", "Laid-back")


# ---------------------------------------------------------------------------
# playlists router
# ---------------------------------------------------------------------------


def test_create_playlist_via_http_calls_music_client_and_persists_youtube_id(
    session, api_client, fake_music_client
):
    _seed_default_user(session)

    response = api_client.post(
        "/api/v1/playlists",
        json={"name": "Late Night Chill", "description": "mellow songs"},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["playlist"]["name"] == "Late Night Chill"
    fake_music_client.create_playlist.assert_called_once_with("Late Night Chill", "mellow songs")

    from app.repositories.playlist_repository import PlaylistRepository

    persisted = PlaylistRepository(session).get(body["playlist"]["id"])
    assert persisted.youtube_playlist_id == "yt-playlist-fake-id"


def test_create_playlist_via_http_propagates_music_client_failure(
    session, api_client, fake_music_client
):
    """Documents current behavior: playlists.py has no domain-exception
    mapping of its own (unlike review-queue), so a music_client failure
    propagates as an unhandled error rather than a clean 4xx/502 — not one
    of this review's findings to remap, just verified here so a future
    change to this route's error handling has a test to update."""
    _seed_default_user(session)
    fake_music_client.create_playlist.side_effect = RuntimeError("cookie auth expired")

    with pytest.raises(RuntimeError):
        api_client.post(
            "/api/v1/playlists",
            json={"name": "Late Night Chill", "description": "mellow songs"},
            headers=_CSRF_HEADERS,
        )


# ---------------------------------------------------------------------------
# CSRF guard (app/main.py) — exercised here since it applies to every router
# ---------------------------------------------------------------------------


def test_mutating_request_without_csrf_header_is_rejected(session, api_client):
    user = _seed_default_user(session)
    _, _, queue_item = _seed_review_queue_item(session, user)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/reject", json={"expected_version": 1}
    )

    assert response.status_code == 403


def test_mutating_request_from_disallowed_origin_is_rejected(session, api_client):
    user = _seed_default_user(session)
    _, _, queue_item = _seed_review_queue_item(session, user)

    response = api_client.post(
        f"/api/v1/review-queue/{queue_item.id}/reject",
        json={"expected_version": 1},
        headers={**_CSRF_HEADERS, "Origin": "https://evil.example.com"},
    )

    assert response.status_code == 403


def test_get_request_does_not_require_csrf_header(session, api_client):
    response = api_client.get("/api/v1/review-queue")

    assert response.status_code == 200
