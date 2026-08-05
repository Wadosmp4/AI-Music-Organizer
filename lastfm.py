"""Genre lookup via Last.fm's artist.getTopTags — free API, key from last.fm/api/account/create.

Set the key via LASTFM_API_KEY env var, or write it to lastfm_key.txt next to this file.
Results are cached to lastfm_genre_cache.json so re-runs don't re-query the same artist.
"""

import json
import os
import time
from pathlib import Path

import requests

API_KEY_ENV = "LASTFM_API_KEY"
API_KEY_FILE = "lastfm_key.txt"
CACHE_FILE = "lastfm_genre_cache.json"
API_URL = "https://ws.audioscrobbler.com/2.0/"

# Last.fm tags are user-submitted and noisy; skip these when picking a genre.
TAG_DENYLIST = {
    "seen live", "favorites", "favorite", "favourite", "awesome", "beautiful",
    "amazing", "love", "love it", "check out", "spotify", "male vocalists",
    "female vocalists", "under 2000 listeners", "cool", "good", "great", "best",
    "80s", "90s", "00s", "classic",
}
MIN_TAG_WEIGHT = 20  # Last.fm tag weight is 0-100


def _api_key():
    key = os.environ.get(API_KEY_ENV)
    if key:
        return key
    if Path(API_KEY_FILE).exists():
        return Path(API_KEY_FILE).read_text().strip()
    return None


def _load_cache():
    if Path(CACHE_FILE).exists():
        return json.loads(Path(CACHE_FILE).read_text())
    return {}


def _save_cache(cache):
    Path(CACHE_FILE).write_text(json.dumps(cache, indent=2, ensure_ascii=False))


class GenreLookup:
    """Looks up a primary genre tag per artist via Last.fm, with local caching."""

    def __init__(self):
        self.api_key = _api_key()
        self.cache = _load_cache()

    @property
    def enabled(self):
        return self.api_key is not None

    def genre_for(self, artist):
        if not self.enabled:
            return None
        if artist in self.cache:
            return self.cache[artist]

        genre = self._fetch(artist)
        self.cache[artist] = genre
        _save_cache(self.cache)
        time.sleep(0.25)  # stay well under Last.fm's rate limit
        return genre

    def _fetch(self, artist):
        try:
            resp = requests.get(
                API_URL,
                params={
                    "method": "artist.gettoptags",
                    "artist": artist,
                    "autocorrect": 1,
                    "api_key": self.api_key,
                    "format": "json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            tags = resp.json().get("toptags", {}).get("tag", [])
        except (requests.RequestException, ValueError):
            return None

        for tag in tags:
            name = (tag.get("name") or "").strip()
            weight = int(tag.get("count") or 0)
            if name and name.lower() not in TAG_DENYLIST and weight >= MIN_TAG_WEIGHT:
                return name
        return None
