"""Server-side web OAuth against the official YouTube Data API v3 (KTD4).

Replaces the legacy `run_local_server` browser-popup flow, which cannot
work for a persistent backend or a remote mobile client. This class
exposes an authorization URL to redirect the user to, and a
`exchange_code_for_token` step for the callback to call — wiring the
actual HTTP redirect/callback routes is an API-layer concern outside
this integration client (see app/api/v1/auth_youtube.py).
"""

import re
from pathlib import Path
from threading import Lock
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.core.config import get_settings
from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import Track
from app.integrations.http_client import CircuitBreaker, call_with_retry

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
UNAVAILABLE_TITLES = {"Private video", "Deleted video"}
MUSIC_CATEGORY_ID = "10"  # YouTube's official video category taxonomy
_CHANNEL_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$")


class _PendingOAuthState:
    """Single in-flight OAuth `state` token (CSRF protection for the login
    flow). This is a personal single-user tool with no session/cookie store
    (KTD2) — a bare in-memory slot is sufficient because only one browser
    completes this flow at a time; the callback must present the exact state
    this process handed out at /authorize, one-time-use."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state: Optional[str] = None

    def issue(self, state: str) -> None:
        with self._lock:
            self._state = state

    def consume(self, state: str) -> bool:
        """Clears the pending state only on an actual match. A wrong guess
        (an attacker probing the callback, or a stale/duplicate request)
        must not invalidate a real in-flight flow — only the true state
        value consumes the slot, so the legitimate callback can still
        complete afterward."""
        with self._lock:
            matches = self._state is not None and self._state == state
            if matches:
                self._state = None
            return matches


pending_oauth_state = _PendingOAuthState()


class YouTubeDataApiClient:
    def __init__(self, token_file: Optional[str] = None):
        settings = get_settings()
        self.client_id = settings.yt_data_api_client_id
        self.client_secret = settings.yt_data_api_client_secret
        self.redirect_uri = settings.yt_data_api_redirect_uri
        self.token_file = Path(token_file or settings.yt_data_api_token_file)
        self._circuit_breaker = CircuitBreaker()

    def _client_config(self) -> dict:
        return {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [self.redirect_uri],
            }
        }

    def get_authorization_url(self) -> tuple[str, str]:
        """Returns (authorization_url, state). The caller must persist `state`
        (via `pending_oauth_state.issue`) and verify it against whatever the
        callback receives before calling `exchange_code_for_token` — Google's
        own oauthlib generates this state for CSRF protection; previously it
        was silently discarded here, so the callback (once wired up) would
        have had nothing to check the returned state against.
        """
        flow = Flow.from_client_config(
            self._client_config(), scopes=SCOPES, redirect_uri=self.redirect_uri
        )
        auth_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        return auth_url, state

    def exchange_code_for_token(self, code: str) -> None:
        flow = Flow.from_client_config(
            self._client_config(), scopes=SCOPES, redirect_uri=self.redirect_uri
        )
        flow.fetch_token(code=code)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(flow.credentials.to_json())
        auth_status_store.set_detection_status(AuthStatus.OK)

    def _load_credentials(self) -> Credentials:
        if not self.token_file.exists():
            auth_status_store.set_detection_status(
                AuthStatus.NEEDS_RECONNECT, "no token on file — connect an account"
            )
            raise RuntimeError("YouTube Data API not connected — no token on file")

        creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds.valid:
            return creds

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                # Refresh itself failing means the token was revoked, not just expired —
                # this gets its own manual-reconnect affordance (KTD17); refresh alone can't recover it.
                auth_status_store.set_detection_status(
                    AuthStatus.NEEDS_RECONNECT, f"token revoked, refresh failed: {exc}"
                )
                raise
            self.token_file.write_text(creds.to_json())
            auth_status_store.set_detection_status(AuthStatus.OK)
            return creds

        auth_status_store.set_detection_status(
            AuthStatus.NEEDS_RECONNECT, "token invalid and not refreshable"
        )
        raise RuntimeError("YouTube Data API token invalid and not refreshable")

    def get_liked_songs(self) -> list[Track]:
        creds = self._load_credentials()
        youtube = build("youtube", "v3", credentials=creds)

        channel = call_with_retry(
            lambda: youtube.channels().list(part="contentDetails", mine=True).execute(),
            circuit_breaker=self._circuit_breaker,
        )
        liked_playlist_id = channel["items"][0]["contentDetails"]["relatedPlaylists"]["likes"]

        tracks: list[Track] = []
        page_token = None
        while True:
            resp = call_with_retry(
                lambda pt=page_token: youtube.playlistItems()
                .list(playlistId=liked_playlist_id, part="snippet", maxResults=50, pageToken=pt)
                .execute(),
                circuit_breaker=self._circuit_breaker,
            )
            for item in resp.get("items", []):
                snippet = item["snippet"]
                title = snippet.get("title", "")
                video_id = snippet.get("resourceId", {}).get("videoId")
                if not video_id or title in UNAVAILABLE_TITLES:
                    continue
                artist = _CHANNEL_TOPIC_SUFFIX.sub(
                    "", snippet.get("videoOwnerChannelTitle") or "Unknown Artist"
                )
                tracks.append({"videoId": video_id, "title": title, "artists": [{"name": artist}]})
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        music_ids = self._music_video_ids(youtube, [t["videoId"] for t in tracks])
        return [t for t in tracks if t["videoId"] in music_ids]

    def _music_video_ids(self, youtube, video_ids: list[str]) -> set[str]:
        """Liked videos include regular (non-music) YouTube likes; keep only official Music category."""
        music_ids: set[str] = set()
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = call_with_retry(
                lambda b=batch: youtube.videos().list(id=",".join(b), part="snippet").execute(),
                circuit_breaker=self._circuit_breaker,
            )
            for item in resp.get("items", []):
                if item["snippet"].get("categoryId") == MUSIC_CATEGORY_ID:
                    music_ids.add(item["id"])
        return music_ids
