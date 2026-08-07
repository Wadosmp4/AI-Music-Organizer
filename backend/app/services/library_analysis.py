"""Onboarding library analysis and new-playlist selection (U9, F5, R7).

Distinct from U3's per-song classification hot path: this batches *every*
library item pulled in so far through the LLM at once (ported from
organize_music.py's suggest_playlists) to find clusters of thematically
related songs worth proposing as brand-new playlists — name, theme, and an
estimated song count only. Deliberately not limited to still-unplaced songs:
a song can already fit an existing playlist and still belong in a newly
proposed one too (multi-label matching means accepting a new proposal never
removes it from where it already landed). No songs are attached and no
review_queue items are created here (R8) — that's U4's backfill, which this
unit deliberately gates: only after the user's selection completes does
onboarding_completed_at get set, which U4 checks before it starts classifying
(F5 step 4).
"""

from dataclasses import dataclass
from datetime import timezone
from typing import Optional

import litellm
from litellm import completion
from pydantic import BaseModel
from sqlmodel import Session

from app.core.db import get_engine
from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.integrations.youtube_data_api_client import track_artist
from app.models.base import utcnow
from app.models.playlist import Playlist
from app.models.reorganize_session import ReorganizeSession
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.services.genre_lookup import GenreLookupService

litellm.suppress_debug_info = True

CLUSTERING_MODEL = "openrouter/google/gemini-2.5-flash"
BATCH_SIZE = 150  # a single call over thousands of tracks overflows the model's output budget
MIN_CLUSTER_SIZE = 4  # ported from organize_music.py — minimum songs to justify a new playlist

# KTD9: if clustering_status is "in_progress" but no PlaylistProposal row has
# been persisted for a reorganize session in longer than this, the poll
# endpoint reports "stalled" instead of leaving the frontend polling a dead
# background task forever.
STALLED_THRESHOLD_SECONDS = 120

SYSTEM_PROMPT = """
You are organizing a YouTube Music library into playlists.

You will get a numbered list of songs (artist, title, and a rough genre tag when
known). Group them into a small number of thematically coherent playlists using
your own knowledge of these artists/songs — genre, mood, era, or language. The
genre tag is only a hint; it may be missing or wrong.

Rules:
- Cluster by genre, mood, or theme, never by artist identity. A good playlist
  spans multiple different artists that share a real musical throughline —
  it is not just "everything by this one artist."
- Do not propose a playlist whose songs are all by the same single artist
  unless every one of those songs plainly has no other thematic home. Prefer
  merging a small same-artist group into a broader genre/mood cluster with
  other artists over proposing it as its own playlist.
- Only propose a playlist for a group of at least 4 clearly related songs.
- Give each playlist a short, human-friendly name (e.g. "90s R&B", "Ukrainian Rock", "Chill Electronic").
- Give each playlist a one-sentence theme description a listener could recognize the vibe from.
- Every song index must appear in at most one playlist.
- Leave out songs that don't fit well anywhere — do not force weak groupings.
- Only use indices that were given to you; never invent songs.
"""


class _PlaylistSuggestion(BaseModel):
    name: str
    theme: str
    indices: list[int]


class _PlaylistSuggestions(BaseModel):
    playlists: list[_PlaylistSuggestion]


@dataclass
class PlaylistProposal:
    name: str
    theme: str
    song_count: int
    confidence: float


@dataclass
class ReorganizeStatus:
    """Poll-endpoint response shape (U2) -- distinct from the DB-backed
    `app.models.reorganize_session.PlaylistProposal` row: this carries only
    what the frontend needs to render (name/theme/count), filtered to rows
    that have crossed MIN_CLUSTER_SIZE.
    """

    session_id: int
    clustering_status: str
    proposals: list


def _format_tracks(tracks: list[dict]) -> str:
    lines = []
    for i, t in enumerate(tracks):
        genre = f" [{t['genre']}]" if t.get("genre") else ""
        lines.append(f"{i}: {t['artist']} - {t['title']}{genre}")
    return "\n".join(lines)


def _format_existing_playlists_context(playlists: list[Playlist]) -> str:
    """Shows the LLM the genre/mood granularity this user already organizes
    by (e.g. "EDM mix", "Cardio", "Classic") so new proposals match that
    style instead of degrading to a same-artist bin when a song's genre tag
    is missing or too generic to group on its own."""
    described = [p for p in playlists if p.description]
    if not described:
        return ""
    lines = [f"- {p.name}: {p.description}" for p in described]
    return (
        "This user already organizes their library into playlists like these "
        "(genre/mood-based, spanning many artists each) — match this style and "
        "granularity for any new proposals rather than grouping by artist:\n"
        + "\n".join(lines)
    )


def _cluster_batch(
    circuit_breaker: CircuitBreaker, batch: list[dict], existing_context: str = ""
) -> list[dict]:
    """Module-level (not an instance method) so the background reorganize
    clustering runner (U2) can call it without constructing a full
    LibraryAnalysisService -- it never needs `music_client` or any
    repository, only an LLM call and a circuit breaker to guard it."""
    schema = _PlaylistSuggestions.model_json_schema()
    user_content = _format_tracks(batch)
    if existing_context:
        user_content = f"{existing_context}\n\n{user_content}"

    def _call():
        return completion(
            model=CLUSTERING_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "playlist_suggestions", "schema": schema, "strict": True},
            },
            temperature=0.0,
            max_tokens=8000,
        )

    try:
        resp = call_with_retry(
            _call,
            retries=2,
            delay_s=1.0,
            retry_on=(litellm.exceptions.APIError,),
            circuit_breaker=circuit_breaker,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _PlaylistSuggestions.model_validate_json(raw.strip())
    except Exception as exc:
        # Caught broadly and deliberately, matching the sibling LLM call
        # sites in bpm_lookup.py/classification.py (KTD18): an empty
        # `choices` list (e.g. a safety-filtered response) raises IndexError
        # on `resp.choices[0]`, which a narrow except tuple wouldn't cover.
        # A batch-level clustering failure just leaves those songs
        # unclustered for this pass — never blocks the rest of the run. Own
        # health-store key (distinct from BPM-estimate/description-match,
        # KTD17) so one LLM use case's failure can't mask another's.
        dependency_health_store.set_status(
            "llm_clustering", DependencyStatus.DEGRADED, f"clustering batch failed: {exc}"
        )
        return []

    dependency_health_store.set_status("llm_clustering", DependencyStatus.OK)
    used = set()
    results = []
    for suggestion in parsed.playlists:
        indices = [i for i in suggestion.indices if 0 <= i < len(batch) and i not in used]
        used.update(indices)
        if indices:
            results.append({"name": suggestion.name, "theme": suggestion.theme, "count": len(indices)})
    return results


def run_reorganize_clustering(
    reorganize_session_id: int, liked_songs: list[dict], engine=None
) -> None:
    """Background task (U2, KTD9): triggered via FastAPI's `BackgroundTasks`
    after the trigger endpoint responds, so it must open its own DB session
    rather than reusing the (already-closed) request session -- the first
    background-job pattern in this codebase (see Risks & Dependencies).
    `engine` defaults to the process-wide engine (`get_engine()`); tests pass
    their isolated test engine explicitly.

    Clusters the session's snapshot in BATCH_SIZE batches, persisting each
    batch's results as PlaylistProposal rows incrementally (via
    `merge_proposal`) so the poll endpoint has something durable to read as
    soon as the first batch completes, instead of blocking on the whole
    library. A batch-level clustering failure (see `_cluster_batch`) doesn't
    stop later batches from running.
    """
    engine = engine or get_engine()
    with Session(engine) as db_session:
        reorganize_session_repo = ReorganizeSessionRepository(db_session)
        playlist_repo = PlaylistRepository(db_session)
        genre_lookup = GenreLookupService(db_session)

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is None:
            return

        snapshot_ids = set(reorganize_session.video_id_snapshot)
        tracks = [song for song in liked_songs if song.get("videoId") in snapshot_ids]
        enriched = [
            {
                "videoId": track["videoId"],
                "title": track.get("title", ""),
                "artist": track_artist(track),
                "genre": genre_lookup.genre_for(track_artist(track)) if genre_lookup.enabled else None,
            }
            for track in tracks
        ]
        existing_context = _format_existing_playlists_context(
            playlist_repo.list_for_user(reorganize_session.user_id)
        )

        circuit_breaker = CircuitBreaker()
        for i in range(0, len(enriched), BATCH_SIZE):
            batch = enriched[i : i + BATCH_SIZE]
            for suggestion in _cluster_batch(circuit_breaker, batch, existing_context):
                reorganize_session_repo.merge_proposal(
                    reorganize_session_id,
                    suggestion["name"],
                    suggestion["theme"],
                    suggestion["count"],
                )

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is not None:
            reorganize_session.clustering_status = "done"
            reorganize_session_repo.update(reorganize_session)


class LibraryAnalysisService:
    def __init__(
        self,
        library_repository: LibraryRepository,
        playlist_repository: PlaylistRepository,
        review_queue_repository: ReviewQueueRepository,
        user_repository: UserRepository,
        music_client: MusicServiceClient,
        reorganize_session_repository: Optional[ReorganizeSessionRepository] = None,
        genre_lookup: Optional[GenreLookupService] = None,
        openrouter_api_key: Optional[str] = None,
    ):
        self.library_repository = library_repository
        self.playlist_repository = playlist_repository
        self.review_queue_repository = review_queue_repository
        self.user_repository = user_repository
        self.music_client = music_client
        self.reorganize_session_repository = reorganize_session_repository
        self.genre_lookup = genre_lookup
        self.openrouter_api_key = openrouter_api_key
        self._circuit_breaker = CircuitBreaker()

    def list_added_playlists(self, user_id: int) -> list[Playlist]:
        """Playlists this app has already created (a prior onboarding run or
        the create-by-description feature) — distinct from playlists that
        exist on YouTube Music but this app has never touched (see
        `list_existing_youtube_playlists`)."""
        return self.playlist_repository.list_for_user(user_id)

    def list_existing_youtube_playlists(self, user_id: int) -> list[dict]:
        """Real YouTube Music playlists the user already had before using this
        tool — i.e. every library playlist minus the ones this app already
        created (tracked locally by `youtube_playlist_id`). A live external
        call: on failure this degrades to an empty list (KTD18) rather than
        blocking the rest of onboarding analysis, since the AI proposals and
        already-added playlists are still useful without it.
        """
        added = self.playlist_repository.list_for_user(user_id)
        tracked_ids = {p.youtube_playlist_id for p in added if p.youtube_playlist_id}
        try:
            youtube_playlists = self.music_client.get_library_playlists()
        except Exception:
            return []
        return [p for p in youtube_playlists if p["playlistId"] not in tracked_ids]

    def trigger_reorganize(self, user_id: int) -> tuple[ReorganizeSession, list[dict]]:
        """R1/R2: fetches the user's complete current liked-songs library
        (not just what's already ingested) and reuses-or-creates their open
        ReorganizeSession (KTD2), merging in any newly-liked video ids so a
        repeat trigger re-covers the whole library rather than starting a
        second concurrent session. Returns the session plus the raw fetched
        songs so the caller can hand both to the background clustering
        runner without a second live fetch.
        """
        reorganize_session = self.reorganize_session_repository.get_open_for_user(user_id)
        liked_songs = self.music_client.get_liked_songs()
        liked_video_ids = [s["videoId"] for s in liked_songs if s.get("videoId")]

        if reorganize_session is None:
            reorganize_session = self.reorganize_session_repository.create(
                ReorganizeSession(
                    user_id=user_id,
                    video_id_snapshot=liked_video_ids,
                    clustering_status="in_progress",
                )
            )
        else:
            existing_ids = set(reorganize_session.video_id_snapshot)
            reorganize_session.video_id_snapshot = reorganize_session.video_id_snapshot + [
                video_id for video_id in liked_video_ids if video_id not in existing_ids
            ]
            reorganize_session.clustering_status = "in_progress"
            reorganize_session = self.reorganize_session_repository.update(reorganize_session)

        return reorganize_session, liked_songs

    def get_reorganize_status(self, reorganize_session_id: int) -> Optional["ReorganizeStatus"]:
        """Poll-endpoint read (U2, KTD9): reports proposals accumulated so
        far and the session's clustering progress, downgrading a stale
        "in_progress" (no PlaylistProposal persisted recently, and the
        session itself hasn't been touched recently) to "stalled" so the
        frontend can offer a re-trigger instead of polling forever. Computed
        at read time rather than written by the background task, so a
        crashed background task doesn't need its own recovery step here.
        """
        reorganize_session = self.reorganize_session_repository.get(reorganize_session_id)
        if reorganize_session is None:
            return None

        proposals = [
            p
            for p in self.reorganize_session_repository.list_proposals(reorganize_session_id)
            if p.song_count >= MIN_CLUSTER_SIZE
        ]

        clustering_status = reorganize_session.clustering_status
        if clustering_status == "in_progress":
            all_proposals = self.reorganize_session_repository.list_proposals(reorganize_session_id)
            last_activity = max(
                [p.updated_at for p in all_proposals] + [reorganize_session.updated_at]
            )
            # SQLite doesn't persist tzinfo (KTD -- round-tripped datetimes
            # come back naive even with DateTime(timezone=True)); treat a
            # naive value as UTC rather than raising on the subtraction below.
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            age_seconds = (utcnow() - last_activity).total_seconds()
            if age_seconds > STALLED_THRESHOLD_SECONDS:
                clustering_status = "stalled"

        return ReorganizeStatus(
            session_id=reorganize_session.id,
            clustering_status=clustering_status,
            proposals=proposals,
        )

    def propose_new_playlists(self, user_id: int) -> list[PlaylistProposal]:
        if not self.openrouter_api_key:
            return []

        library_items = self.library_repository.list_for_user(user_id)
        library_items = [item for item in library_items if item.removed_at is None]
        if not library_items:
            return []

        enriched = [
            {
                "videoId": item.video_id,
                "title": item.title,
                "artist": item.artist,
                "genre": self.genre_lookup.genre_for(item.artist) if self.genre_lookup else None,
            }
            for item in library_items
        ]
        existing_context = _format_existing_playlists_context(
            self.playlist_repository.list_for_user(user_id)
        )

        merged: dict[str, dict] = {}
        for i in range(0, len(enriched), BATCH_SIZE):
            batch = enriched[i : i + BATCH_SIZE]
            for suggestion in _cluster_batch(self._circuit_breaker, batch, existing_context):
                key = suggestion["name"].strip().lower()
                if key in merged:
                    merged[key]["count"] += suggestion["count"]
                else:
                    merged[key] = suggestion

        return [
            PlaylistProposal(
                name=s["name"],
                theme=s["theme"],
                song_count=s["count"],
                confidence=min(0.5 + 0.05 * s["count"], 0.9),
            )
            for s in merged.values()
            if s["count"] >= MIN_CLUSTER_SIZE
        ]

    def complete_onboarding(
        self,
        user_id: int,
        accepted_proposals: list[dict],
        custom_playlists: list[dict],
        adopted_playlists: Optional[list[dict]] = None,
        removed_playlist_ids: Optional[list[int]] = None,
    ) -> list[Playlist]:
        """Creates the real YouTube Music playlist and its empty local record
        per accepted proposal and per custom addition (F5 step 3) — never
        attaches songs, never creates review_queue items. A record without a
        `youtube_playlist_id` could never be approved/moved into later, so
        the YouTube-side create happens here, not deferred to first approve.

        `adopted_playlists` are playlists that already exist on YouTube
        Music (surfaced by `list_existing_youtube_playlists`) that the user
        chose to bring under this app's management — no YouTube-side create,
        just a local record linked to the existing `playlist_id` so
        classification (`run_ingestion_check`'s `candidates`) and manual
        approve/move can target it like any app-created playlist.

        `removed_playlist_ids` are the reverse: already-tracked playlists the
        user unchecked in onboarding, meaning "stop managing this one." Only
        the local record and its dangling review_queue references are
        cleared -- the real playlist and its songs on YouTube are never
        touched. Any review_queue_item still pointing at it (matched or
        manually assigned, at any status) has its playlist_id reset to None
        rather than left dangling, since the local Playlist row it named is
        gone.

        Marks onboarding complete so U4's backfill is allowed to start.
        """
        for playlist_id in removed_playlist_ids or []:
            playlist = self.playlist_repository.get(playlist_id)
            if playlist is None or playlist.user_id != user_id:
                continue
            self.review_queue_repository.clear_playlist_references(playlist_id)
            self.playlist_repository.delete(playlist)

        created = []
        for adopted in adopted_playlists or []:
            created.append(
                self.playlist_repository.create(
                    Playlist(
                        user_id=user_id,
                        name=adopted["name"],
                        description=None,
                        rule=None,
                        youtube_playlist_id=adopted["playlist_id"],
                    )
                )
            )
        for proposal in accepted_proposals:
            name = proposal["name"]
            description = proposal.get("theme")
            youtube_playlist_id = self.music_client.create_playlist(name, description or "")
            created.append(
                self.playlist_repository.create(
                    Playlist(
                        user_id=user_id,
                        name=name,
                        description=description,
                        rule=None,
                        youtube_playlist_id=youtube_playlist_id,
                    )
                )
            )
        for custom in custom_playlists:
            name = custom["name"]
            description = custom.get("description")
            youtube_playlist_id = self.music_client.create_playlist(name, description or "")
            created.append(
                self.playlist_repository.create(
                    Playlist(
                        user_id=user_id,
                        name=name,
                        description=description,
                        rule=None,
                        youtube_playlist_id=youtube_playlist_id,
                    )
                )
            )

        self.user_repository.mark_onboarding_completed(user_id)
        return created
