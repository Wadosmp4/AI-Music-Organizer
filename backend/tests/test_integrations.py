from unittest.mock import MagicMock, patch

import pytest

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.http_client import CircuitBreaker
from app.integrations.youtube_data_api_client import YouTubeDataApiClient
from app.integrations.ytmusic_client import YTMusicClient, artist_bucket_key


@pytest.fixture(autouse=True)
def reset_auth_status():
    auth_status_store.set_write_status(AuthStatus.OK)
    auth_status_store.set_detection_status(AuthStatus.OK)
    yield
    auth_status_store.set_write_status(AuthStatus.OK)
    auth_status_store.set_detection_status(AuthStatus.OK)


def test_cookie_auth_failure_flips_write_path_to_needs_reconnect(tmp_path):
    auth_file = tmp_path / "browser.json"
    auth_file.write_text("{}")

    with patch("app.integrations.ytmusic_client.YTMusic") as mock_ytmusic_cls:
        mock_yt = MagicMock()
        mock_yt.get_library_playlists.side_effect = RuntimeError("signed out")
        mock_ytmusic_cls.return_value = mock_yt

        client = YTMusicClient(auth_file=str(auth_file), data_api_client=MagicMock())

        with pytest.raises(RuntimeError):
            client.get_library_playlists()

    status, reason = auth_status_store.get_write_status()
    assert status == AuthStatus.NEEDS_RECONNECT
    assert "signed out" in reason


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
    status, _ = auth_status_store.get_detection_status()
    assert status == AuthStatus.OK


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
