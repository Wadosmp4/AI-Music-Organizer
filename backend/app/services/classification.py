"""Classifies a track against candidate playlists with confidence + explanation.

A song can belong to more than one playlist -- classify_track returns a
*list* of matches, one per playlist it fits, not a single best guess.
Precedence only matters within a single playlist's own evaluation (KTD10): a
playlist's explicit rule is a hard gate -- when present, its description and
artist history are never used for that playlist. Absent a rule, a playlist
matches this song if the LLM vibe/description match includes it OR its
artist-similarity score clears MIN_MATCH_SCORE -- independently, so the same
song can land in an EDM-vibed playlist AND a workout playlist that happens to
already contain plenty of that same artist. A per-song failure in any
external call (KTD18) degrades gracefully (that signal contributes no
matches) rather than raising and blocking the caller's next song.

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

from app.integrations.base import MusicServiceClient, Track
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.integrations.youtube_data_api_client import artist_bucket_key, track_artist
from app.models.playlist import Playlist
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.playlist_repository import PlaylistRepository
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


def fetch_playlist_tracks(music_client: MusicServiceClient, playlist: Playlist) -> list[Track]:
    """A playlist's *actual* current YouTube content, fetched fresh on every
    call rather than cached locally, so it reflects songs added outside this
    app (an adopted pre-existing playlist's whole back catalog) as well as
    ones this app approved/moved in. Degrades to an empty list on any fetch
    failure or a playlist with no youtube_playlist_id (KTD18) -- one
    playlist's fetch problem must not block classification against every
    other candidate.
    """
    if not playlist.youtube_playlist_id:
        return []
    try:
        return music_client.get_playlist_tracks(playlist.youtube_playlist_id)
    except Exception:
        return []


def artist_counts_from_tracks(tracks: list[Track]) -> dict[str, int]:
    """Tallies a playlist's real content by artist bucket (ported from
    organize_music.py's build_plan) -- the signal artist-similarity
    classification scores each candidate against."""
    counts: dict[str, int] = {}
    for track in tracks:
        key = artist_bucket_key(track)
        counts[key] = counts.get(key, 0) + 1
    return counts


def build_artist_counts(music_client: MusicServiceClient, playlist: Playlist) -> dict[str, int]:
    return artist_counts_from_tracks(fetch_playlist_tracks(music_client, playlist))


def build_candidate(
    music_client: MusicServiceClient,
    classification_service: "ClassificationService",
    playlist_repository: PlaylistRepository,
    playlist: Playlist,
) -> CandidatePlaylist:
    """The one place a `Playlist` becomes a classify_track candidate: fetches
    its real YouTube content once, backfills a missing description by
    generating one from that content (a playlist adopted from an
    already-populated pre-existing YouTube playlist starts with no
    description of its own -- see LibraryAnalysisService.complete_onboarding
    -- so without this it could only ever match via artist repetition), then
    derives artist_counts from the same fetch. A description generated here
    is persisted immediately so it's reused (and refined by nothing further)
    on every later classification pass, not regenerated every call.
    """
    tracks = fetch_playlist_tracks(music_client, playlist)
    if playlist.description is None and tracks:
        generated = classification_service.generate_vibe_description(tracks)
        if generated:
            playlist = playlist_repository.update_description(playlist.id, generated) or playlist
    return CandidatePlaylist.from_playlist(playlist, artist_counts=artist_counts_from_tracks(tracks))


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
    matched_playlists: list[str] = []


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
    ) -> list[ClassificationResult]:
        """Returns one ClassificationResult per playlist this song fits --
        zero playlists matching still returns exactly one result, with
        playlist_id=None, so the caller always has something to queue."""
        artist = track_artist(track)
        genre = self.genre_lookup.genre_for(artist)
        bpm_result = self.bpm_lookup.lookup_bpm(artist, track.get("title", ""))
        results: list[ClassificationResult] = []

        def _result(playlist_id: Optional[int], confidence: float, explanation: Explanation) -> ClassificationResult:
            return ClassificationResult(
                playlist_id=playlist_id,
                confidence=confidence,
                explanation=explanation,
                bpm=bpm_result.bpm,
                bpm_source=bpm_result.source,
                genre=genre,
            )

        # KTD10: a ruled playlist is judged on its rule alone -- never
        # reconsidered via artist-similarity or description below. Every
        # ruled playlist whose rule matches is its own independent result.
        ruled_playlists = [p for p in candidate_playlists if p.rule]
        for playlist in ruled_playlists:
            if _rule_matches(playlist.rule, genre, bpm_result.bpm):
                results.append(
                    _result(
                        playlist.id,
                        0.95,
                        Explanation("rule", f"matched the explicit rule on '{playlist.name}'"),
                    )
                )

        unruled_playlists = [p for p in candidate_playlists if not p.rule]

        # Vibe/theme fit (LLM description match): every playlist whose
        # description this song genuinely fits, not just the single best one
        # -- a song can belong to more than one playlist.
        description_matches = self._match_description(track, unruled_playlists)
        for playlist in description_matches:
            results.append(
                _result(
                    playlist.id,
                    0.6,
                    Explanation(
                        "description_match", f"description of '{playlist.name}' matched this song"
                    ),
                )
            )
        description_matched_ids = {p.id for p in description_matches}

        # Artist-similarity + correction feedback: every remaining unruled
        # playlist that clears MIN_MATCH_SCORE, independently -- not just
        # whichever scores highest. A playlist already matched via
        # description above is skipped here so it isn't reported twice.
        key = artist_bucket_key(track)
        # U7/R15: when unset (the default), this is `{}` and every line below
        # behaves exactly as it did before this parameter existed.
        correction_boosts = self._correction_boosts(user_id, key, genre)
        for playlist in unruled_playlists:
            if playlist.id in description_matched_ids:
                continue
            base_score = playlist.artist_counts.get(key, 0)
            score = base_score + correction_boosts.get(playlist.id, 0)
            if score < MIN_MATCH_SCORE:
                continue
            if base_score >= MIN_MATCH_SCORE:
                # Existing-song history alone already clears the bar —
                # identical to pre-U7 behavior, correction feedback or not.
                signal = "artist_similarity"
                detail = f"{base_score} existing songs by this artist already in '{playlist.name}'"
            else:
                # A recent correction (KTD12: bounded recent-N, same
                # artist/genre cluster) is what tipped this playlist over the
                # threshold — name it as its own signal rather than
                # overstating existing-song history that isn't there.
                signal = "correction_feedback"
                detail = (
                    f"a recent correction for this artist/genre moved a similar song to '{playlist.name}'"
                )
            results.append(_result(playlist.id, min(0.5 + 0.1 * score, 0.95), Explanation(signal, detail)))

        if not results:
            results.append(
                _result(
                    None,
                    0.0,
                    Explanation("none", "no rule, artist-similarity, or description match found"),
                )
            )
        return results

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
    ) -> list[CandidatePlaylist]:
        candidates = [p for p in playlists if p.description]
        if not candidates or not self.openrouter_api_key:
            return []

        try:
            matched_names = self._match_description_with_llm(track, candidates)
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
            return []

        dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)
        matched_name_set = set(matched_names)
        return [p for p in candidates if p.name in matched_name_set]

    def _match_description_with_llm(self, track: Track, candidates: list[CandidatePlaylist]) -> list[str]:
        targets = "\n".join(f'- "{p.name}": {p.description}' for p in candidates)
        schema = _DescriptionMatch.model_json_schema()

        def _call():
            return completion(
                model=DESCRIPTION_MATCH_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Decide which playlist(s) (by name) this song matches well, based on "
                            "each playlist's description. A song can genuinely fit more than one "
                            "playlist -- list every playlist it's a strong, confident fit for. "
                            "Only include playlists you're confident about; if none fit well, "
                            "return an empty list. Only use playlist names exactly as given; "
                            "never invent one."
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
        return parsed.matched_playlists

    def generate_vibe_description(self, tracks: list[Track]) -> Optional[str]:
        """One-sentence vibe/theme summary of a playlist's real content
        (artist/title pairs), used to seed `description_match` for a playlist
        adopted from an already-populated real YouTube playlist -- those have
        no description of their own, so without this they can never match on
        anything but artist-repetition (a much weaker signal, KTD-vibe).
        Degrades to None on any LLM failure or missing key (KTD18) — the
        caller just skips persisting a description and can retry on a later
        classification pass."""
        if not self.openrouter_api_key or not tracks:
            return None
        listing = "\n".join(f"{track_artist(t)} - {t.get('title', '')}" for t in tracks[:100])

        def _call():
            return completion(
                model=DESCRIPTION_MATCH_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are given a list of songs (artist - title) already in a "
                            "playlist. Write ONE short sentence describing the playlist's "
                            "vibe/theme (mood, genre, era, or language) a listener could use "
                            "to judge whether a new song fits. Reply with only that sentence, "
                            "no preamble or quotes."
                        ),
                    },
                    {"role": "user", "content": listing},
                ],
                temperature=0.2,
                max_tokens=100,
            )

        try:
            resp = call_with_retry(
                _call,
                retries=2,
                delay_s=1.0,
                retry_on=(litellm.exceptions.APIError,),
                circuit_breaker=self._circuit_breaker,
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            dependency_health_store.set_status(
                "llm_description_match",
                DependencyStatus.DEGRADED,
                f"vibe description generation failed: {exc}",
            )
            return None
        return text or None
