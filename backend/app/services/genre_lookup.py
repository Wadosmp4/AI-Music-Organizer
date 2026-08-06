"""Genre lookup via Last.fm's artist.getTopTags, DB-backed cache (KTD23).

Ports lastfm.py's logic, replacing its lastfm_genre_cache.json file cache
with GenreCacheRepository so re-runs (and every user, since genre tags are
shared reference data) don't re-query the same artist.
"""

import requests
from sqlmodel import Session

from app.core.config import get_settings
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, CircuitOpenError, call_with_retry
from app.repositories.genre_cache_repository import GenreCacheRepository

API_URL = "https://ws.audioscrobbler.com/2.0/"

# Last.fm tags are user-submitted and noisy; skip these when picking a genre.
TAG_DENYLIST = {
    "seen live", "favorites", "favorite", "favourite", "awesome", "beautiful",
    "amazing", "love", "love it", "check out", "spotify", "male vocalists",
    "female vocalists", "under 2000 listeners", "cool", "good", "great", "best",
    "80s", "90s", "00s", "classic",
}
MIN_TAG_WEIGHT = 20  # Last.fm tag weight is 0-100


class GenreLookupService:
    def __init__(self, session: Session, api_key: str | None = None):
        self.repository = GenreCacheRepository(session)
        self.api_key = api_key if api_key is not None else get_settings().lastfm_api_key
        self._circuit_breaker = CircuitBreaker()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def genre_for(self, artist: str) -> str | None:
        if not self.enabled:
            return None

        cached = self.repository.get(artist)
        if cached is not None:
            return cached.genre

        try:
            genre = self._fetch(artist)
        except (requests.RequestException, ValueError, CircuitOpenError) as exc:
            # A timeout/rate-limit/tripped-breaker is distinguishable from a
            # genuine miss (KTD18): surface degraded health, but don't cache a
            # failure as "no genre." CircuitOpenError must be caught here too
            # — it's raised by call_with_retry's circuit_breaker.before_call(),
            # not by the request itself, so it isn't a requests.RequestException.
            dependency_health_store.set_status(
                "lastfm", DependencyStatus.DEGRADED, f"Last.fm lookup failed: {exc}"
            )
            return None

        dependency_health_store.set_status("lastfm", DependencyStatus.OK)
        self.repository.upsert(artist, genre)
        return genre

    def _fetch(self, artist: str) -> str | None:
        resp = call_with_retry(
            lambda: requests.get(
                API_URL,
                params={
                    "method": "artist.gettoptags",
                    "artist": artist,
                    "autocorrect": 1,
                    "api_key": self.api_key,
                    "format": "json",
                },
                timeout=10,
            ),
            retries=2,
            delay_s=1.0,
            retry_on=(requests.RequestException,),
            circuit_breaker=self._circuit_breaker,
        )
        resp.raise_for_status()
        tags = resp.json().get("toptags", {}).get("tag", [])

        for tag in tags:
            name = (tag.get("name") or "").strip()
            weight = int(tag.get("count") or 0)
            if name and name.lower() not in TAG_DENYLIST and weight >= MIN_TAG_WEIGHT:
                return name
        return None
