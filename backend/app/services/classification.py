"""Classifies a track against candidate playlists with confidence + explanation.

Precedence (KTD10): a playlist's explicit rule is a hard gate — when present,
its description is never used for candidacy on that playlist. Absent a rule,
artist-similarity against existing playlist contents (ported from
organize_music.py's build_plan) is tried first, then an LLM description
match. A per-song failure in any external call (KTD18) degrades gracefully
to the next signal rather than raising and blocking the caller's next song.

Correction feedback (U7/R15/KTD12): when a `CorrectionLogRepository` is
wired in, a bounded recent-N of that user's past corrections for the same
artist/genre cluster contribute a bonus to the artist-similarity score of
the playlist the user actually moved the song *to*. This only shifts
*future* classify_track calls — it never re-evaluates other pending queue
items, and it never touches a ruled playlist's outcome (KTD10 untouched).
"""

from dataclasses import dataclass
from typing import Optional

import litellm
from litellm import completion
from pydantic import BaseModel

from app.integrations.base import Track
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.integrations.youtube_data_api_client import artist_bucket_key, track_artist
from app.models.playlist import Playlist
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.services.bpm_lookup import BpmLookupService
from app.services.genre_lookup import GenreLookupService

litellm.suppress_debug_info = True

DESCRIPTION_MATCH_MODEL = "openrouter/google/gemini-2.5-flash"
MIN_MATCH_SCORE = 2  # ported from organize_music.py

# KTD12: feedback is bounded to a fixed recent-N of corrections, never the
# user's whole history.
CORRECTION_LOOKBACK_LIMIT = 20
# Weight of a single matching recent correction in the artist-similarity
# score, expressed in the same units as `artist_counts` (existing-song
# tallies). Set equal to MIN_MATCH_SCORE so one relevant correction alone is
# enough to surface a destination playlist that otherwise has zero
# existing-song history with this artist.
CORRECTION_BONUS_PER_MATCH = MIN_MATCH_SCORE


@dataclass
class CandidatePlaylist:
    id: int
    name: str
    rule: Optional[dict]
    description: Optional[str]
    artist_counts: dict[str, int]  # artist_bucket_key -> count of existing songs by that artist

    @classmethod
    def from_playlist(cls, playlist: Playlist, artist_counts: Optional[dict[str, int]] = None) -> "CandidatePlaylist":
        return cls(
            id=playlist.id,
            name=playlist.name,
            rule=playlist.rule,
            description=playlist.description,
            artist_counts=artist_counts or {},
        )


@dataclass
class Explanation:
    signal: str  # "rule" | "artist_similarity" | "correction_feedback" | "description_match" | "none"
    detail: str


@dataclass
class ClassificationResult:
    playlist_id: Optional[int]
    confidence: float
    explanation: Explanation
    bpm: Optional[float]
    bpm_source: Optional[str]
    genre: Optional[str]

    def as_explanation_dict(self) -> dict:
        return {
            "signal": self.explanation.signal,
            "detail": self.explanation.detail,
            "bpm": self.bpm,
            "bpm_source": self.bpm_source,
            "genre": self.genre,
        }


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


def build_correction_context(track: Track, genre: Optional[str]) -> dict:
    """Context payload for a `CorrectionLogEntry` logged at review-time.

    Populates the fields `_correction_boosts` matches future classify_track
    calls on: the artist bucket key (same normalization used for
    artist-similarity, so channel-name variants of one artist still collide)
    and the genre at the time of the correction. Callers that log
    corrections (e.g. a review-queue move) may merge additional keys in.
    """
    return {"artist_bucket_key": artist_bucket_key(track), "artist": track_artist(track), "genre": genre}


class ClassificationService:
    def __init__(
        self,
        genre_lookup: GenreLookupService,
        bpm_lookup: BpmLookupService,
        openrouter_api_key: Optional[str] = None,
        correction_log_repo: Optional[CorrectionLogRepository] = None,
    ):
        self.genre_lookup = genre_lookup
        self.bpm_lookup = bpm_lookup
        self.openrouter_api_key = openrouter_api_key or bpm_lookup.openrouter_api_key
        # U7/R15: additive and optional. When None (the default — unchanged for
        # every existing two-positional-arg caller), classify_track's behavior
        # is byte-for-byte identical to before this parameter existed.
        self.correction_log_repo = correction_log_repo
        self._circuit_breaker = CircuitBreaker()

    def classify_track(
        self,
        track: Track,
        candidate_playlists: list[CandidatePlaylist],
        user_id: Optional[int] = None,
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
        # U7/R15: when unset (the default), this is `{}` and every line below
        # behaves exactly as it did before this parameter existed — `score`
        # equals `base_score` for every playlist, so `best_signal` is always
        # "artist_similarity", unchanged from before.
        correction_boosts = self._correction_boosts(user_id, key, genre)
        best_playlist, best_score, best_base_score = None, 0, 0
        for playlist in unruled_playlists:
            base_score = playlist.artist_counts.get(key, 0)
            score = base_score + correction_boosts.get(playlist.id, 0)
            if score > best_score:
                best_playlist, best_score, best_base_score = playlist, score, base_score
        if best_playlist and best_score >= MIN_MATCH_SCORE:
            if best_base_score >= MIN_MATCH_SCORE:
                # Existing-song history alone already clears the bar —
                # identical to pre-U7 behavior, correction feedback or not.
                signal = "artist_similarity"
                detail = f"{best_base_score} existing songs by this artist already in '{best_playlist.name}'"
            else:
                # A recent correction (KTD12: bounded recent-N, same
                # artist/genre cluster) is what tipped this playlist over the
                # threshold — name it as its own signal rather than
                # overstating existing-song history that isn't there.
                signal = "correction_feedback"
                detail = (
                    f"a recent correction for this artist/genre moved a similar song to '{best_playlist.name}'"
                )
            return ClassificationResult(
                playlist_id=best_playlist.id,
                confidence=min(0.5 + 0.1 * best_score, 0.95),
                explanation=Explanation(signal, detail),
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

    def _correction_boosts(
        self, user_id: Optional[int], key: str, genre: Optional[str]
    ) -> dict[int, int]:
        """Additive artist-similarity bonus per destination playlist id (U7/R15).

        Reads a bounded recent-N (KTD12) of this user's past corrections and
        tallies, per `corrected_playlist_id` (the playlist the user actually
        moved the song *to*), how many of those recent corrections were for
        the same artist bucket or the same genre as the song being classified
        right now. Read-only: this never mutates a `ReviewQueueItem` or any
        other row, and never re-evaluates other pending queue items — it only
        shapes the score computed for *this* classify_track call.

        Returns `{}` when no repo is wired in, when no user_id was given, or
        when no recent correction matches — in every one of those cases the
        caller's scoring loop behaves exactly as it did before this method
        existed.
        """
        if self.correction_log_repo is None or user_id is None:
            return {}

        corrections = self.correction_log_repo.list_recent_for_user(
            user_id, limit=CORRECTION_LOOKBACK_LIMIT
        )
        boosts: dict[int, int] = {}
        for entry in corrections:
            if entry.corrected_playlist_id is None:
                continue
            context = entry.context or {}
            same_artist = context.get("artist_bucket_key") == key
            entry_genre = context.get("genre")
            same_genre = (
                genre is not None
                and entry_genre is not None
                and str(entry_genre).lower() == genre.lower()
            )
            if same_artist or same_genre:
                boosts[entry.corrected_playlist_id] = (
                    boosts.get(entry.corrected_playlist_id, 0) + CORRECTION_BONUS_PER_MATCH
                )
        return boosts

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
            # Own health-store key (distinct from BPM-estimate/clustering,
            # KTD17) so one LLM use case's failure can't mask another's.
            dependency_health_store.set_status(
                "llm_description_match", DependencyStatus.DEGRADED, f"description match failed: {exc}"
            )
            return None

        dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)
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
