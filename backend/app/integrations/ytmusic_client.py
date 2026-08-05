"""Sole `MusicServiceClient` implementation for now (KTD25).

Internally composes two YouTube-specific auth mechanisms: cookie auth
(`ytmusicapi`, the write path) for playlist reads/writes, and the official
Data API (the detection path, `YouTubeDataApiClient`) for reliably listing
liked songs — ytmusicapi's own liked-songs endpoint gets intermittently
soft-blocked. A future second service would not need this internal split;
it's a YouTube-specific detail hidden behind this one adapter (R1).
"""

import re
from pathlib import Path
from typing import Optional

from ytmusicapi import YTMusic

from app.core.config import get_settings
from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import MusicServiceClient, PlaylistSummary, Track
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.integrations.youtube_data_api_client import YouTubeDataApiClient

_ARTIST_NOISE = re.compile(r"(?:[\s\-(]*(?:vevo|official))+\)?\s*$", re.IGNORECASE)


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


class YTMusicClient(MusicServiceClient):
    def __init__(
        self,
        auth_file: Optional[str] = None,
        data_api_client: Optional[YouTubeDataApiClient] = None,
    ):
        settings = get_settings()
        self._auth_file = Path(auth_file or settings.ytmusic_auth_file)
        self._data_api_client = data_api_client or YouTubeDataApiClient()
        self._circuit_breaker = CircuitBreaker()

    def _client(self) -> YTMusic:
        if not self._auth_file.exists():
            auth_status_store.set_write_status(
                AuthStatus.NEEDS_RECONNECT, "no cookie auth file — run the reconnect flow"
            )
            raise RuntimeError(f"YouTube Music auth file not found: {self._auth_file}")
        return YTMusic(str(self._auth_file))

    def _call(self, fn):
        try:
            result = call_with_retry(
                fn, retries=3, delay_s=2.0, circuit_breaker=self._circuit_breaker
            )
        except Exception as exc:
            auth_status_store.set_write_status(
                AuthStatus.NEEDS_RECONNECT, f"YouTube Music write path failing: {exc}"
            )
            raise
        auth_status_store.set_write_status(AuthStatus.OK)
        return result

    def get_liked_songs(self) -> list[Track]:
        return self._data_api_client.get_liked_songs()

    def get_library_playlists(self) -> list[PlaylistSummary]:
        yt = self._client()
        return self._call(lambda: yt.get_library_playlists(limit=None))

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        yt = self._client()
        details = self._call(lambda: yt.get_playlist(playlist_id, limit=None))
        return details.get("tracks", [])

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> None:
        yt = self._client()
        self._call(lambda: yt.add_playlist_items(playlist_id, video_ids, duplicates=False))

    def create_playlist(self, name: str, description: str) -> str:
        yt = self._client()
        return self._call(lambda: yt.create_playlist(name, description))
