"""Sole `MusicServiceClient` implementation (KTD4, KTD25), backed entirely by
the official YouTube Data API v3 -- both the detection path (listing liked
songs) and the write path (creating playlists, adding items) go through this
one server-side web OAuth credential.

The write path used to go through `ytmusicapi` (first cookie auth, then an
attempt to reuse this same OAuth token as a Bearer header). That reuse
attempt was abandoned: `music.youtube.com`'s internal API rejects Bearer
tokens from this OAuth client's type outright (HTTP 400, even for a plain
read), which matches `ytmusicapi`'s own OAuth support only ever working with
a Google-registered "TVs and Limited Input devices" client -- a client type
that only supports the device-code grant, the exact flow already confirmed
broken upstream (see the Stop Conditions in the product plan). The official
Data API's `playlists`/`playlistItems` endpoints are fully supported for this
OAuth client's type, so the write path now uses them directly instead.
"""

import re
from pathlib import Path
from threading import Lock
from typing import Iterator, Optional

from contextlib import contextmanager

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import get_settings
from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import MusicServiceClient, PlaylistSummary, QuotaExceededError, Track
from app.integrations.http_client import CircuitBreaker, call_with_retry

SCOPES = ["https://www.googleapis.com/auth/youtube"]
"""Full read-write scope, not `.readonly` -- this one credential now backs
both the detection path and the write path. Anyone with an existing
`.readonly`-scoped token on file must re-run the /authorize login flow once
to grant the wider scope."""
UNAVAILABLE_TITLES = {"Private video", "Deleted video"}
MUSIC_CATEGORY_ID = "10"  # YouTube's official video category taxonomy
_CHANNEL_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$")
_ARTIST_NOISE = re.compile(r"(?:[\s\-(]*(?:vevo|official))+\)?\s*$", re.IGNORECASE)

QUOTA_EXCEEDED_MESSAGE = (
    "YouTube API daily quota exceeded. This resets at midnight Pacific Time -- "
    "try again after it resets."
)


def track_artist(track: Track) -> str:
    artists = track.get("artists") or []
    return artists[0]["name"] if artists else "Unknown Artist"


def artist_bucket_key(track: Track) -> str:
    """Normalized key for merging channel-name variants of the same artist
    (e.g. "Twenty One Pilots" vs "twenty one pilots" vs "TwentyOnePilotsVEVO").

    Real VEVO channel names are conventionally squashed with no spaces
    (e.g. "KatyPerryVEVO"), so stripping the suffix alone isn't enough to
    collide it with the spaced-out artist name — whitespace is removed
    too, after the suffix strip, so both variants land on the same key.
    """
    stripped = _ARTIST_NOISE.sub("", track_artist(track)).strip().lower()
    return re.sub(r"\s+", "", stripped)


_QUOTA_EXCEEDED_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
"""YouTube Data API v3 error `reason` values that mean the quota window
itself is exhausted, as opposed to some other 403 (missing scope, ACL
denial) that a retry or reconnect could plausibly fix."""


def _is_quota_exceeded(exc: HttpError) -> bool:
    if exc.status_code != 403:
        return False
    details = exc.error_details
    return isinstance(details, list) and any(
        isinstance(d, dict) and d.get("reason") in _QUOTA_EXCEEDED_REASONS for d in details
    )


def _execute(request):
    """Executes a googleapiclient request, translating a quota-exhausted
    403 into `QuotaExceededError` (base.py) so `call_with_retry`'s
    `non_retryable` can short-circuit it instead of burning three more
    calls against the same exhausted quota, and so `_status_tracking`
    below can tell it apart from an actual credential failure.
    """
    try:
        return request.execute()
    except HttpError as exc:
        if _is_quota_exceeded(exc):
            raise QuotaExceededError(QUOTA_EXCEEDED_MESSAGE) from exc
        raise


def _track_from_playlist_item(item: dict) -> Optional[Track]:
    snippet = item["snippet"]
    title = snippet.get("title", "")
    video_id = snippet.get("resourceId", {}).get("videoId")
    if not video_id or title in UNAVAILABLE_TITLES:
        return None
    artist = _CHANNEL_TOPIC_SUFFIX.sub(
        "", snippet.get("videoOwnerChannelTitle") or "Unknown Artist"
    )
    return {"videoId": video_id, "title": title, "artists": [{"name": artist}]}


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


class YouTubeDataApiClient(MusicServiceClient):
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
        # No include_granted_scopes: it makes Google bundle any previously
        # granted scope (e.g. a stale .readonly grant from before SCOPES was
        # widened) in with this request's scope, and oauthlib's strict
        # scope-match check on token exchange then rejects the combined
        # response because it doesn't equal exactly what we asked for.
        auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
        return auth_url, state

    def exchange_code_for_token(self, code: str) -> None:
        flow = Flow.from_client_config(
            self._client_config(), scopes=SCOPES, redirect_uri=self.redirect_uri
        )
        flow.fetch_token(code=code)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(flow.credentials.to_json())
        # Both paths share this one credential -- a fresh successful login
        # clears both banners, not just detection's (KTD17: independently
        # surfaced, but a shared root cause fixes both at once).
        auth_status_store.set_detection_status(AuthStatus.OK)
        auth_status_store.set_write_status(AuthStatus.OK)

    def _load_credentials(self) -> Credentials:
        """Loads and, if necessary, refreshes the stored OAuth credential.

        Pure credential mechanics -- raises on missing/invalid/unrefreshable
        tokens, with no opinion on which call path (detection vs write)
        triggered the load. Callers go through `_status_tracking` so the
        failure is attributed to the right health signal (KTD17).
        """
        if not self.token_file.exists():
            raise RuntimeError("YouTube Data API not connected — no token on file")

        creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds.valid:
            return creds

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                # Refresh itself failing means the token was revoked, not just expired --
                # this gets its own manual-reconnect affordance (KTD17); refresh alone can't recover it.
                raise RuntimeError(f"token revoked, refresh failed: {exc}") from exc
            self.token_file.write_text(creds.to_json())
            return creds

        raise RuntimeError("YouTube Data API token invalid and not refreshable")

    @contextmanager
    def _status_tracking(self, status_setter):
        """Runs a block against the shared OAuth credential, translating its
        outcome into the given health-status setter (KTD17): NEEDS_RECONNECT
        with the failure reason on any exception, OK otherwise. Which setter
        (`set_detection_status` vs `set_write_status`) is picked by the
        caller, since that's the only thing that distinguishes the two call
        paths now that they share one credential.
        """
        try:
            yield
        except QuotaExceededError:
            # Quota exhaustion says nothing about this OAuth credential --
            # reconnecting won't free up quota, so leave the health signal
            # as-is rather than sending the user down the wrong recovery
            # path (KTD17). Callers see the real exception either way.
            raise
        except Exception as exc:
            status_setter(AuthStatus.NEEDS_RECONNECT, str(exc))
            raise
        else:
            status_setter(AuthStatus.OK)

    def _paginate_items(self, request_fn) -> Iterator[dict]:
        """Yields every `items` entry across all pages of a List call.

        request_fn(page_token) -> unexecuted request object; `_execute`
        below runs it so a quota-exceeded 403 is translated before it ever
        reaches `call_with_retry`'s retry logic.
        """
        page_token = None
        while True:
            resp = call_with_retry(
                lambda pt=page_token: _execute(request_fn(pt)),
                circuit_breaker=self._circuit_breaker,
                non_retryable=(QuotaExceededError,),
            )
            yield from resp.get("items", [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def get_liked_songs(self) -> list[Track]:
        with self._status_tracking(auth_status_store.set_detection_status):
            creds = self._load_credentials()
            youtube = build("youtube", "v3", credentials=creds)

            channel = call_with_retry(
                lambda: _execute(youtube.channels().list(part="contentDetails", mine=True)),
                circuit_breaker=self._circuit_breaker,
                non_retryable=(QuotaExceededError,),
            )
            liked_playlist_id = channel["items"][0]["contentDetails"]["relatedPlaylists"]["likes"]

            # TEST-ONLY: simulates a smaller library for local end-to-end
            # testing without touching the real liked-songs list. Breaks out
            # of the page loop itself (not just a post-hoc slice) once the
            # cap is reached -- _paginate_items is a lazy generator, so
            # stopping consumption here means later pages are never
            # requested at all, which is the whole point under quota
            # pressure: a capped test run should cost roughly
            # ceil(test_limit / 50) quota units, not one page per ~2800
            # real liked songs.
            test_limit = get_settings().liked_songs_test_limit
            tracks: list[Track] = []
            for item in self._paginate_items(
                lambda pt: youtube.playlistItems().list(
                    playlistId=liked_playlist_id, part="snippet", maxResults=50, pageToken=pt
                )
            ):
                track = _track_from_playlist_item(item)
                if track is not None:
                    tracks.append(track)
                if test_limit > 0 and len(tracks) >= test_limit:
                    break

            music_ids = self._music_video_ids(youtube, [t["videoId"] for t in tracks])
            return [t for t in tracks if t["videoId"] in music_ids]

    def _music_video_ids(self, youtube, video_ids: list[str]) -> set[str]:
        """Liked videos include regular (non-music) YouTube likes; keep only official Music category."""
        music_ids: set[str] = set()
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = call_with_retry(
                lambda b=batch: _execute(youtube.videos().list(id=",".join(b), part="snippet")),
                circuit_breaker=self._circuit_breaker,
                non_retryable=(QuotaExceededError,),
            )
            for item in resp.get("items", []):
                if item["snippet"].get("categoryId") == MUSIC_CATEGORY_ID:
                    music_ids.add(item["id"])
        return music_ids

    def get_library_playlists(self) -> list[PlaylistSummary]:
        with self._status_tracking(auth_status_store.set_write_status):
            creds = self._load_credentials()
            youtube = build("youtube", "v3", credentials=creds)
            items = self._paginate_items(
                lambda pt: youtube.playlists().list(
                    part="snippet", mine=True, maxResults=50, pageToken=pt
                )
            )
            return [
                {"playlistId": item["id"], "title": item["snippet"]["title"]} for item in items
            ]

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        with self._status_tracking(auth_status_store.set_write_status):
            creds = self._load_credentials()
            youtube = build("youtube", "v3", credentials=creds)
            items = self._paginate_items(
                lambda pt: youtube.playlistItems().list(
                    playlistId=playlist_id, part="snippet", maxResults=50, pageToken=pt
                )
            )
            return [t for item in items if (t := _track_from_playlist_item(item)) is not None]

    def create_playlist(self, name: str, description: str) -> str:
        with self._status_tracking(auth_status_store.set_write_status):
            creds = self._load_credentials()
            youtube = build("youtube", "v3", credentials=creds)
            response = call_with_retry(
                lambda: _execute(
                    youtube.playlists().insert(
                        part="snippet,status",
                        body={
                            "snippet": {"title": name, "description": description},
                            "status": {"privacyStatus": "private"},
                        },
                    )
                ),
                circuit_breaker=self._circuit_breaker,
                non_retryable=(QuotaExceededError,),
            )
            return response["id"]

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> None:
        with self._status_tracking(auth_status_store.set_write_status):
            creds = self._load_credentials()
            youtube = build("youtube", "v3", credentials=creds)
            for video_id in video_ids:
                call_with_retry(
                    lambda vid=video_id: _execute(
                        youtube.playlistItems().insert(
                            part="snippet",
                            body={
                                "snippet": {
                                    "playlistId": playlist_id,
                                    "resourceId": {"kind": "youtube#video", "videoId": vid},
                                }
                            },
                        )
                    ),
                    circuit_breaker=self._circuit_breaker,
                    non_retryable=(QuotaExceededError,),
                )
