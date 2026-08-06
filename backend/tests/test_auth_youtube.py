"""YouTube Data API OAuth login flow (app/api/v1/auth_youtube.py, KTD4)."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.integrations.youtube_data_api_client import YouTubeDataApiClient, pending_oauth_state
from app.main import app

client = TestClient(app)


def test_authorize_redirects_to_google_and_stores_state():
    with patch.object(
        YouTubeDataApiClient,
        "get_authorization_url",
        return_value=("https://accounts.google.com/o/oauth2/auth?fake=1", "fake-state-123"),
    ):
        response = client.get("/api/v1/auth/youtube/authorize", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "https://accounts.google.com/o/oauth2/auth?fake=1"
    # The state issued above is now the one and only thing /callback will accept.
    assert pending_oauth_state.consume("fake-state-123") is True


def test_authorize_without_configured_credentials_redirects_with_reason():
    unconfigured_client = MagicMock(client_id="", client_secret="")
    with patch(
        "app.api.v1.auth_youtube.YouTubeDataApiClient", return_value=unconfigured_client
    ):
        response = client.get("/api/v1/auth/youtube/authorize", follow_redirects=False)

    unconfigured_client.get_authorization_url.assert_not_called()
    assert response.status_code in (302, 307)
    assert "youtube_connect=not_configured" in response.headers["location"]


def test_callback_with_matching_state_exchanges_code_and_redirects_success():
    with patch.object(
        YouTubeDataApiClient, "get_authorization_url", return_value=("http://fake", "state-abc")
    ):
        client.get("/api/v1/auth/youtube/authorize", follow_redirects=False)

    with patch.object(YouTubeDataApiClient, "exchange_code_for_token") as mock_exchange:
        response = client.get(
            "/api/v1/auth/youtube/callback",
            params={"code": "auth-code-xyz", "state": "state-abc"},
            follow_redirects=False,
        )

    mock_exchange.assert_called_once_with("auth-code-xyz")
    assert response.status_code in (302, 307)
    assert "youtube_connect=success" in response.headers["location"]


def test_callback_with_mismatched_state_is_rejected_without_exchanging_code():
    with patch.object(
        YouTubeDataApiClient, "get_authorization_url", return_value=("http://fake", "real-state")
    ):
        client.get("/api/v1/auth/youtube/authorize", follow_redirects=False)

    with patch.object(YouTubeDataApiClient, "exchange_code_for_token") as mock_exchange:
        response = client.get(
            "/api/v1/auth/youtube/callback",
            params={"code": "auth-code-xyz", "state": "attacker-supplied-state"},
            follow_redirects=False,
        )

    mock_exchange.assert_not_called()
    assert response.status_code in (302, 307)
    assert "youtube_connect=state_mismatch" in response.headers["location"]
    # The legitimate state is still pending (a mismatched guess must not
    # consume/invalidate it) -- the real callback can still complete.
    assert pending_oauth_state.consume("real-state") is True


def test_callback_with_no_pending_state_is_rejected():
    pending_oauth_state.consume("anything")  # ensure nothing is pending
    response = client.get(
        "/api/v1/auth/youtube/callback",
        params={"code": "auth-code-xyz", "state": "some-state"},
        follow_redirects=False,
    )

    assert "youtube_connect=state_mismatch" in response.headers["location"]


def test_callback_with_google_error_param_is_treated_as_denied():
    response = client.get(
        "/api/v1/auth/youtube/callback",
        params={"error": "access_denied", "state": "irrelevant"},
        follow_redirects=False,
    )

    assert "youtube_connect=denied" in response.headers["location"]


def test_callback_exchange_failure_redirects_with_failed_reason():
    with patch.object(
        YouTubeDataApiClient, "get_authorization_url", return_value=("http://fake", "state-fail")
    ):
        client.get("/api/v1/auth/youtube/authorize", follow_redirects=False)

    with patch.object(
        YouTubeDataApiClient, "exchange_code_for_token", side_effect=RuntimeError("bad code")
    ):
        response = client.get(
            "/api/v1/auth/youtube/callback",
            params={"code": "auth-code-xyz", "state": "state-fail"},
            follow_redirects=False,
        )

    assert "youtube_connect=failed" in response.headers["location"]


def test_callback_is_not_blocked_by_the_csrf_guard():
    """The CSRF guard in app/main.py only gates mutating methods -- Google's
    redirect to /callback is a plain GET and must never need the custom
    X-Requested-With header the frontend sends on POST/PUT/PATCH/DELETE."""
    response = client.get(
        "/api/v1/auth/youtube/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )

    assert response.status_code in (302, 307)  # not 403
