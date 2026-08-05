"""Classifies a track against candidate playlists with confidence + explanation.

Precedence (KTD10): a playlist's explicit rule is a hard gate — when present,
its description is never used for candidacy on that playlist. Absent a rule,
artist-similarity against existing playlist contents (ported from
organize_music.py's build_plan) is tried first, then an LLM description
match. A per-song failure in any external call (KTD18) degrades gracefully
to the next signal rather than raising and blocking the caller's next song.
"""

from dataclasses import dataclass
from typing import Optional

import litellm
from litellm import completion
from pydantic import BaseModel

from app.integrations.base import Track
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.integrations.ytmusic_client import artist_bucket_key, track_artist
from app.services.bpm_lookup import BpmLookupService
from app.services.genre_lookup import GenreLookupService

litellm.suppress_debug_info = True

DESCRIPTION_MATCH_MODEL = "openrouter/google/gemini-2.5-flash"
MIN_MATCH_SCORE = 2  # ported from organize_music.py


@dataclass
class CandidatePlaylist:
    id: int
    name: str
    rule: Optional[dict]
    description: Optional[str]
    artist_counts: dict[str, int]  # artist_bucket_key -> count of existing songs by that artist


@dataclass
class Explanation:
    signal: str  # "rule" | "artist_similarity" | "description_match" | "none"
    detail: str


@dataclass
class ClassificationResult:
    playlist_id: Optional[int]
    confidence: float
    explanation: Explanation
    bpm: Optional[float]
    bpm_source: Optional[str]
    genre: Optional[str]


class _DescriptionMatch(BaseModel):
    matched_playlist: Optional[str] = None


def _rule_matches(rule: dict, genre: Optional[str], bpm: Optional[float]) -> bool:
    """A playlist's explicit rule is a hard gate (KTD10): every present condition must hold."""
    if "genre" in rule and (genre is None or rule["genre"].lower() != genre.lower()):
        return False
    if "bpm_min" in rule and (bpm is None or bpm < rule["bpm_min"]):
        return False
    if "bpm_max" in rule and (bpm is None or bpm > rule["bpm_max"]):
        return False
    return True


class ClassificationService:
    def __init__(
        self,
        genre_lookup: GenreLookupService,
        bpm_lookup: BpmLookupService,
        openrouter_api_key: Optional[str] = None,
    ):
        self.genre_lookup = genre_lookup
        self.bpm_lookup = bpm_lookup
        self.openrouter_api_key = openrouter_api_key or bpm_lookup.openrouter_api_key
        self._circuit_breaker = CircuitBreaker()

    def classify_track(
        self, track: Track, candidate_playlists: list[CandidatePlaylist]
    ) -> ClassificationResult:
        artist = track_artist(track)
        genre = self.genre_lookup.genre_for(artist)
        bpm_result = self.bpm_lookup.lookup_bpm(artist, track.get("title", ""))

        # KTD10: a ruled playlist is judged on its rule alone. A ruled playlist
        # that doesn't match is excluded outright — never reconsidered via
        # artist-similarity or description on the fall-through below.
        ruled_playlists = [p for p in candidate_playlists if p.rule]
        for playlist in ruled_playlists:
            if _rule_matches(playlist.rule, genre, bpm_result.bpm):
                return ClassificationResult(
                    playlist_id=playlist.id,
                    confidence=0.95,
                    explanation=Explanation("rule", f"matched the explicit rule on '{playlist.name}'"),
                    bpm=bpm_result.bpm,
                    bpm_source=bpm_result.source,
                    genre=genre,
                )

        unruled_playlists = [p for p in candidate_playlists if not p.rule]

        key = artist_bucket_key(track)
        best_playlist, best_score = None, 0
        for playlist in unruled_playlists:
            score = playlist.artist_counts.get(key, 0)
            if score > best_score:
                best_playlist, best_score = playlist, score
        if best_playlist and best_score >= MIN_MATCH_SCORE:
            return ClassificationResult(
                playlist_id=best_playlist.id,
                confidence=min(0.5 + 0.1 * best_score, 0.95),
                explanation=Explanation(
                    "artist_similarity",
                    f"{best_score} existing songs by this artist already in '{best_playlist.name}'",
                ),
                bpm=bpm_result.bpm,
                bpm_source=bpm_result.source,
                genre=genre,
            )

        description_match = self._match_description(track, unruled_playlists)
        if description_match is not None:
            return ClassificationResult(
                playlist_id=description_match.id,
                confidence=0.6,
                explanation=Explanation(
                    "description_match",
                    f"description of '{description_match.name}' matched this song",
                ),
                bpm=bpm_result.bpm,
                bpm_source=bpm_result.source,
                genre=genre,
            )

        return ClassificationResult(
            playlist_id=None,
            confidence=0.0,
            explanation=Explanation("none", "no rule, artist-similarity, or description match found"),
            bpm=bpm_result.bpm,
            bpm_source=bpm_result.source,
            genre=genre,
        )

    def _match_description(
        self, track: Track, playlists: list[CandidatePlaylist]
    ) -> Optional[CandidatePlaylist]:
        candidates = [p for p in playlists if p.description]
        if not candidates or not self.openrouter_api_key:
            return None

        try:
            matched_name = self._match_description_with_llm(track, candidates)
        except Exception as exc:
            # KTD18: a single song's failure here — network error, timeout,
            # rate limit, malformed response — fails only this song's
            # description-match stage. Caught broadly and deliberately: this is
            # the per-song fault-isolation boundary, so it falls through to "no
            # match" rather than raising and blocking the caller's next song.
            dependency_health_store.set_status(
                "llm", DependencyStatus.DEGRADED, f"description match failed: {exc}"
            )
            return None

        dependency_health_store.set_status("llm", DependencyStatus.OK)
        if not matched_name:
            return None
        return next((p for p in candidates if p.name == matched_name), None)

    def _match_description_with_llm(self, track: Track, candidates: list[CandidatePlaylist]) -> Optional[str]:
        targets = "\n".join(f'- "{p.name}": {p.description}' for p in candidates)
        schema = _DescriptionMatch.model_json_schema()

        def _call():
            return completion(
                model=DESCRIPTION_MATCH_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Decide which playlist (by name) this song best matches, based on "
                            "the playlist's description. Only match on a strong, confident fit — "
                            'if none fit well, return null for matched_playlist. Only use playlist '
                            "names exactly as given; never invent one."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Song: {track_artist(track)} - {track.get('title', '')}\n\nPlaylists:\n{targets}",
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "description_match", "schema": schema, "strict": True},
                },
                temperature=0.0,
                max_tokens=200,
            )

        resp = call_with_retry(
            _call,
            retries=2,
            delay_s=1.0,
            retry_on=(litellm.exceptions.APIError,),
            circuit_breaker=self._circuit_breaker,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _DescriptionMatch.model_validate_json(raw.strip())
        return parsed.matched_playlist
