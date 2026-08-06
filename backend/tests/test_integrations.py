from unittest.mock import MagicMock, patch

import pytest

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.http_client import CircuitBreaker
from app.integrations.youtube_data_api_client import YouTubeDataApiClient, artist_bucket_key


@pytest.fixture(autouse=True)
def reset_auth_status():
    auth_status_store.set_write_status(AuthStatus.OK)
    auth_status_store.set_detection_status(AuthStatus.OK)
    yield
    auth_status_store.set_write_status(AuthStatus.OK)
    auth_status_store.set_detection_status(AuthStatus.OK)


def _client_with_valid_creds(tmp_path) -> YouTubeDataApiClient:
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    client = YouTubeDataApiClient(token_file=str(token_file))
    client._load_credentials = MagicMock(return_value=MagicMock())
    return client


def test_create_playlist_calls_data_api_and_returns_id(tmp_path):
    client = _client_with_valid_creds(tmp_path)

    with patch("app.integrations.youtube_data_api_client.build") as mock_build:
        mock_youtube = MagicMock()
        mock_youtube.playlists.return_value.insert.return_value.execute.return_value = {
            "id": "PL123"
        }
        mock_build.return_value = mock_youtube

        playlist_id = client.create_playlist("Road Trip", "songs for driving")

    assert playlist_id == "PL123"
    _, kwargs = mock_youtube.playlists.return_value.insert.call_args
    assert kwargs["body"]["snippet"]["title"] == "Road Trip"
    assert kwargs["body"]["snippet"]["description"] == "songs for driving"
    status, _ = auth_status_store.get_write_status()
    assert status == AuthStatus.OK


def test_create_playlist_failure_flips_write_path_to_needs_reconnect(tmp_path):
    client = _client_with_valid_creds(tmp_path)

    with patch("app.integrations.youtube_data_api_client.build") as mock_build:
        mock_youtube = MagicMock()
        mock_youtube.playlists.return_value.insert.return_value.execute.side_effect = RuntimeError(
            "quota exceeded"
        )
        mock_build.return_value = mock_youtube

        with pytest.raises(RuntimeError):
            client.create_playlist("Road Trip", "")

    status, reason = auth_status_store.get_write_status()
    assert status == AuthStatus.NEEDS_RECONNECT
    assert "quota exceeded" in reason


def test_add_playlist_items_inserts_each_video(tmp_path):
    client = _client_with_valid_creds(tmp_path)

    with patch("app.integrations.youtube_data_api_client.build") as mock_build:
        mock_youtube = MagicMock()
        mock_youtube.playlistItems.return_value.insert.return_value.execute.return_value = {}
        mock_build.return_value = mock_youtube

        client.add_playlist_items("PL123", ["v1", "v2"])

    inserted_video_ids = [
        call.kwargs["body"]["snippet"]["resourceId"]["videoId"]
        for call in mock_youtube.playlistItems.return_value.insert.call_args_list
    ]
    assert inserted_video_ids == ["v1", "v2"]


def test_missing_oauth_token_flips_write_path_to_needs_reconnect(tmp_path):
    client = YouTubeDataApiClient(token_file=str(tmp_path / "missing.json"))

    with patch("app.integrations.youtube_data_api_client.build") as mock_build:
        with pytest.raises(RuntimeError):
            client.create_playlist("Road Trip", "")

    mock_build.assert_not_called()
    status, reason = auth_status_store.get_write_status()
    assert status == AuthStatus.NEEDS_RECONNECT
    assert "no token on file" in reason


def test_get_library_playlists_maps_data_api_response(tmp_path):
    client = _client_with_valid_creds(tmp_path)

    with patch("app.integrations.youtube_data_api_client.build") as mock_build:
        mock_youtube = MagicMock()
        mock_youtube.playlists.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "PL1", "snippet": {"title": "Gym"}}],
        }
        mock_build.return_value = mock_youtube

        playlists = client.get_library_playlists()

    assert playlists == [{"playlistId": "PL1", "title": "Gym"}]


def test_get_playlist_tracks_skips_unavailable_videos(tmp_path):
    client = _client_with_valid_creds(tmp_path)

    with patch("app.integrations.youtube_data_api_client.build") as mock_build:
        mock_youtube = MagicMock()
        mock_youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {
                        "title": "Real Song",
                        "resourceId": {"videoId": "v1"},
                        "videoOwnerChannelTitle": "Some Artist - Topic",
                    }
                },
                {"snippet": {"title": "Private video", "resourceId": {"videoId": "v2"}}},
            ]
        }
        mock_build.return_value = mock_youtube

        tracks = client.get_playlist_tracks("PL123")

    assert tracks == [{"videoId": "v1", "title": "Real Song", "artists": [{"name": "Some Artist"}]}]


def test_exchange_code_for_token_clears_both_health_signals(tmp_path):
    auth_status_store.set_detection_status(AuthStatus.NEEDS_RECONNECT, "stale")
    auth_status_store.set_write_status(AuthStatus.NEEDS_RECONNECT, "stale")

    client = YouTubeDataApiClient(token_file=str(tmp_path / "token.json"))
    mock_flow = MagicMock()
    mock_flow.credentials.to_json.return_value = "{}"

    with patch(
        "app.integrations.youtube_data_api_client.Flow.from_client_config", return_value=mock_flow
    ):
        client.exchange_code_for_token("some-code")

    detection_status, _ = auth_status_store.get_detection_status()
    write_status, _ = auth_status_store.get_write_status()
    assert detection_status == AuthStatus.OK
    assert write_status == AuthStatus.OK


def test_oauth_token_expiry_triggers_refresh(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")

    client = YouTubeDataApiClient(token_file=str(token_file))

    mock_creds = MagicMock(valid=False, expired=True, refresh_token="refresh-me")

    def _refresh(_request):
        mock_creds.valid = True
        mock_creds.to_json.return_value = '{"refreshed": true}'

    mock_creds.refresh.side_effect = _refresh
    mock_creds.to_json.return_value = '{"refreshed": true}'

    with patch(
        "app.integrations.youtube_data_api_client.Credentials.from_authorized_user_file",
        return_value=mock_creds,
    ):
        result = client._load_credentials()

    mock_creds.refresh.assert_called_once()
    assert result is mock_creds


def test_oauth_token_revocation_flips_detection_path_only(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")

    client = YouTubeDataApiClient(token_file=str(token_file))

    mock_creds = MagicMock(valid=False, expired=True, refresh_token="refresh-me")
    mock_creds.refresh.side_effect = RuntimeError("invalid_grant: token revoked")

    with patch(
        "app.integrations.youtube_data_api_client.Credentials.from_authorized_user_file",
        return_value=mock_creds,
    ):
        with pytest.raises(RuntimeError):
            with client._status_tracking(auth_status_store.set_detection_status):
                client._load_credentials()

    detection_status, detection_reason = auth_status_store.get_detection_status()
    write_status, _ = auth_status_store.get_write_status()
    assert detection_status == AuthStatus.NEEDS_RECONNECT
    assert "revoked" in detection_reason
    assert write_status == AuthStatus.OK  # independently surfaced (KTD17) — write path untouched


def test_category_filter_excludes_non_music_video():
    client = YouTubeDataApiClient.__new__(YouTubeDataApiClient)
    client._circuit_breaker = CircuitBreaker()

    mock_youtube = MagicMock()
    mock_youtube.videos.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "music-vid", "snippet": {"categoryId": "10"}},
            {"id": "vlog-vid", "snippet": {"categoryId": "22"}},
        ]
    }

    music_ids = client._music_video_ids(mock_youtube, ["music-vid", "vlog-vid"])

    assert music_ids == {"music-vid"}


def test_artist_bucket_key_normalizes_vevo_suffix_variant():
    plain = {"artists": [{"name": "Twenty One Pilots"}]}
    vevo = {"artists": [{"name": "TwentyOnePilotsVEVO"}]}
    lower = {"artists": [{"name": "twenty one pilots"}]}

    assert artist_bucket_key(vevo) == artist_bucket_key(plain) == artist_bucket_key(lower)
    assert artist_bucket_key(plain) == "twentyonepilots"
