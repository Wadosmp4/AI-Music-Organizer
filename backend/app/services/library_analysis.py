"""Onboarding library analysis and new-playlist selection (U9, F5, R7).

Distinct from U3's per-song classification hot path: this batches the user's
*complete* current liked-songs library, fetched fresh from YouTube (not
whatever happens to already be ingested locally), through the LLM at once
(ported from organize_music.py's suggest_playlists) to find clusters of
thematically related songs worth proposing as brand-new playlists — name,
theme, and an estimated song count only. Deliberately not limited to
still-unplaced songs: a song can already fit an existing playlist and still
belong in a newly proposed one too (multi-label matching means accepting a
new proposal never removes it from where it already landed). No songs are
attached and no review_queue items are created here (R8) — that's U4's
backfill, which this unit deliberately gates: only after the user's
selection completes does onboarding_completed_at get set, which U4 checks
before it starts classifying (F5 step 4). This two-phase split is
deliberate: phase one (this unit) analyzes everything to propose playlists,
phase two (U4's backfill) then processes every song into them — each with
its own progress signal (proposals_processed_count/total_count here,
ingestion_processed_count/total_count there).
"""

from dataclasses import dataclass
from datetime import timezone
from typing import Optional

import litellm
from litellm import completion
from pydantic import BaseModel
from sklearn.cluster import HDBSCAN
from sqlmodel import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.integrations.youtube_data_api_client import track_artist
from app.models.base import utcnow
from app.models.playlist import Playlist
from app.models.reorganize_session import ReorganizeSession
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.onboarding_proposal_repository import OnboardingProposalRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.reorganize_session_repository import ReorganizeSessionRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError
from app.repositories.user_repository import UserRepository
from app.repositories.vector_repository import QdrantVectorRepository, get_vector_repository
from app.repositories.vector_repository import content_hash as _track_content_hash
from app.services.embeddings import embed_texts
from app.services.genre_lookup import GenreLookupService
from app.services.reorganize_apply import ApplyAlreadyInProgressError, ReorganizeSessionNotFoundError

litellm.suppress_debug_info = True

CLUSTERING_MODEL = "openrouter/google/gemini-2.5-flash"
MIN_CLUSTER_SIZE = 4  # ported from organize_music.py — minimum songs to justify a new playlist

# How often run_reorganize_clustering persists enriched_count while working
# through the enrichment phase -- frequent enough for a smooth-looking
# progress bar, infrequent enough not to add up to a lot of individual DB
# commits for a large library.
ENRICHMENT_PROGRESS_CHECKPOINT = 25

# KTD9: if clustering_status is "in_progress" but no PlaylistProposal row has
# been persisted for a reorganize session in longer than this, the poll
# endpoint reports "stalled" instead of leaving the frontend polling a dead
# background task forever.
STALLED_THRESHOLD_SECONDS = 120

# Naming-only prompt (KTD: replaces the old grouping-and-naming prompt) --
# cluster membership is now decided geometrically by HDBSCAN over track
# embeddings (see _hdbscan_clusters), so the LLM's only job left is to
# describe a group it didn't have to invent. This also means there's no
# index-selection game to referee: every song handed to this call already
# belongs in the resulting playlist.
NAMING_SYSTEM_PROMPT = """
You are naming a YouTube Music playlist.

You will get a numbered list of songs (artist, title, and a rough genre tag
when known) that have already been grouped together because their titles,
artists, and genres are similar. Using your own knowledge of these
artists/songs, give this group:
- A short, human-friendly playlist name (e.g. "90s R&B", "Ukrainian Rock", "Chill Electronic").
- A one-sentence theme description a listener could recognize the vibe from.

Base the name and theme on what these specific songs actually have in
common — genre, mood, era, or language. Do not just describe them as
"various" or "mixed" — find the real throughline.
"""


class _ClusterName(BaseModel):
    name: str
    theme: str


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
    # Live enrichment progress -- total_count is the session's full snapshot
    # size (known immediately), enriched_count trails it as the background
    # task works through the library. Both 0 before a session ever starts.
    enriched_count: int
    total_count: int
    # Live matching progress (U4 follow-up) -- matched_count trails
    # total_count the same way, once matching has been triggered.
    matching_status: str
    matched_count: int


@dataclass
class OnboardingProposalsStatus:
    """Poll-endpoint response shape for onboarding's initial AI-suggested
    playlists -- same idea as ReorganizeStatus, but user-scoped instead of
    session-scoped (this runs once per user, before any reorganize session
    exists)."""

    proposals_status: str
    proposals_processed_count: int
    proposals_total_count: int
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


def _track_text(track: dict) -> str:
    genre = f" [{track['genre']}]" if track.get("genre") else ""
    return f"{track.get('artist', '')} - {track.get('title', '')}{genre}"


def _get_or_create_embeddings(
    vector_repo: QdrantVectorRepository, user_id: int, tracks: list[dict]
) -> list[list[float]]:
    """Returns one embedding vector per track, aligned 1:1 with `tracks` by
    index. Reuses whatever's already stored in Qdrant for a track whose
    title/artist/genre haven't changed since the last clustering run (same
    reuse idea as genre_lookup.py's DB-backed cache) and only calls the
    local embedding model for what's new or changed -- a repeat run on a
    mostly-unchanged library only pays to embed the delta.
    """
    vector_repo.ensure_collection()
    existing = vector_repo.get_existing(user_id, [t["videoId"] for t in tracks])

    vectors: list[Optional[list[float]]] = [None] * len(tracks)
    stale_indices = []
    for i, track in enumerate(tracks):
        cached = existing.get(track["videoId"])
        expected_hash = _track_content_hash(
            track.get("title", ""), track.get("artist", ""), track.get("genre")
        )
        if cached is not None and cached.content_hash == expected_hash:
            vectors[i] = cached.vector
        else:
            stale_indices.append(i)

    if stale_indices:
        new_vectors = embed_texts([_track_text(tracks[i]) for i in stale_indices])
        for i, vector in zip(stale_indices, new_vectors):
            vectors[i] = vector
        vector_repo.upsert_tracks(user_id, [tracks[i] for i in stale_indices], new_vectors)

    return vectors  # type: ignore[return-value]


def _hdbscan_clusters(vectors: list[list[float]]) -> list[list[int]]:
    """Groups track indices into clusters over the embedding space -- unlike
    the old per-batch LLM clustering, this sees the *entire* input at once,
    so a theme that used to span two separate 150-song batches (and so got
    split or duplicated) now clusters together in one pass. Tracks that
    don't fit any dense group (HDBSCAN's noise label -1) are left out, same
    as the old prompt's "leave out songs that don't fit well" instruction --
    just discovered geometrically instead of by LLM judgment. No `k` to
    guess: HDBSCAN finds however many natural clusters exist. Embeddings are
    pre-normalized (embeddings.py) so plain Euclidean distance ranks
    identically to cosine distance, avoiding a precomputed distance matrix.

    `cluster_selection_method="leaf"`: verified empirically against a real
    2,794-track library. The default "eom" (excess-of-mass) picks whichever
    split of the density tree is most stable overall, which -- combined with
    `allow_single_cluster` -- let the *root* win outright on that library: a
    single 2,350-song "cluster" swallowing most of the input instead of
    finding its ~110 real genre/mood groups. "leaf" always extracts the
    finest-grained real splits instead of preferring a coarser high-level
    merge, which is what "propose distinct themed playlists" actually wants.
    Tradeoff: on a very small/homogeneous input (a handful of songs with no
    other library to contrast against) HDBSCAN may find no cluster at all
    rather than reporting the whole input as one -- accepted as an inherent
    small-N limitation of density-based clustering rather than trading back
    the large-library correctness bug to paper over it.
    """
    if len(vectors) < MIN_CLUSTER_SIZE:
        return []
    labels = HDBSCAN(
        min_cluster_size=MIN_CLUSTER_SIZE, metric="euclidean", copy=False, cluster_selection_method="leaf"
    ).fit_predict(vectors)
    clusters: dict[int, list[int]] = {}
    for i, label in enumerate(labels):
        if label == -1:
            continue
        clusters.setdefault(int(label), []).append(i)
    return list(clusters.values())


def _name_cluster(
    circuit_breaker: CircuitBreaker, tracks: list[dict], existing_context: str = ""
) -> Optional[dict]:
    """Names/describes a cluster whose membership is already fixed by
    _hdbscan_clusters -- module-level (not an instance method) so the
    background reorganize clustering runner (U2) can call it without
    constructing a full LibraryAnalysisService."""
    schema = _ClusterName.model_json_schema()
    user_content = _format_tracks(tracks)
    if existing_context:
        user_content = f"{existing_context}\n\n{user_content}"

    def _call():
        return completion(
            model=CLUSTERING_MODEL,
            messages=[
                {"role": "system", "content": NAMING_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "cluster_name", "schema": schema, "strict": True},
            },
            temperature=0.0,
            max_tokens=500,
            # No timeout means a single stalled request hangs this call
            # forever -- and since this runs once per cluster in a
            # sequential loop, one hang blocks every cluster still waiting
            # to be named.
            timeout=30,
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
        parsed = _ClusterName.model_validate_json(raw.strip())
    except Exception as exc:
        # Caught broadly and deliberately, matching the sibling LLM call
        # site in classification.py (KTD18): an empty `choices` list (e.g. a
        # safety-filtered response) raises IndexError on `resp.choices[0]`,
        # which a narrow except tuple wouldn't cover. A single cluster's
        # naming failure just leaves it unproposed for this pass -- never
        # blocks the rest of the run. Own health-store key (distinct from
        # description-match, KTD17) so one LLM use case's failure can't
        # mask another's.
        dependency_health_store.set_status(
            "llm_clustering", DependencyStatus.DEGRADED, f"clustering (naming) failed: {exc}"
        )
        return None

    dependency_health_store.set_status("llm_clustering", DependencyStatus.OK)
    return {"name": parsed.name, "theme": parsed.theme}


def _cluster_and_name(
    circuit_breaker: CircuitBreaker,
    vector_repo: QdrantVectorRepository,
    user_id: int,
    tracks: list[dict],
    existing_context: str = "",
):
    """Shared clustering primitive (embed -> HDBSCAN -> name each surviving
    cluster) used by both the Reorganize flow and onboarding's
    propose_new_playlists -- both cluster the user's complete current
    liked-songs library and both faced the identical batch-boundary
    inconsistency under the old per-batch LLM clustering, so they share one
    implementation rather than maintaining two.
    Yields one {"name", "theme", "count"} dict per successfully named
    cluster, in cluster-discovery order, so callers can persist incrementally.
    """
    if not tracks:
        return
    vectors = _get_or_create_embeddings(vector_repo, user_id, tracks)
    for indices in _hdbscan_clusters(vectors):
        cluster_tracks = [tracks[i] for i in indices]
        named = _name_cluster(circuit_breaker, cluster_tracks, existing_context)
        if named is not None:
            yield {"name": named["name"], "theme": named["theme"], "count": len(cluster_tracks)}


def run_reorganize_clustering(
    reorganize_session_id: int, liked_songs: list[dict], engine=None, vector_repo=None
) -> None:
    """Background task (U2, KTD9): triggered via FastAPI's `BackgroundTasks`
    after the trigger endpoint responds, so it must open its own DB session
    rather than reusing the (already-closed) request session -- the first
    background-job pattern in this codebase (see Risks & Dependencies).
    `engine` defaults to the process-wide engine (`get_engine()`); tests pass
    their isolated test engine explicitly. `vector_repo` follows the same
    pattern for the Qdrant client.

    Embeds the session's full snapshot at once and clusters it with HDBSCAN
    (`_cluster_and_name`) -- unlike the old per-BATCH_SIZE-chunk LLM
    clustering, the whole library is seen in one pass, so a theme spanning
    what used to be two separate batches no longer splits or duplicates.
    Each named cluster is persisted immediately (via `merge_proposal`) so the
    poll endpoint has something durable to read as soon as it's ready,
    instead of blocking on the whole library. A single cluster's naming
    failure (see `_name_cluster`) doesn't stop the rest from being proposed.
    """
    engine = engine or get_engine()
    vector_repo = vector_repo or get_vector_repository()
    with Session(engine) as db_session:
        reorganize_session_repo = ReorganizeSessionRepository(db_session)
        playlist_repo = PlaylistRepository(db_session)
        genre_lookup = GenreLookupService(db_session)

        reorganize_session = reorganize_session_repo.get(reorganize_session_id)
        if reorganize_session is None:
            return

        snapshot_ids = set(reorganize_session.video_id_snapshot)
        tracks = [song for song in liked_songs if song.get("videoId") in snapshot_ids]
        reorganize_session.enriched_count = 0
        reorganize_session_repo.update(reorganize_session)

        # Genre lookup, mostly cache hits already -- checkpointed the same
        # way the old BPM phase was, though it rarely needs more than one
        # flush now that genre is the only enrichment step.
        enriched = []
        for i, track in enumerate(tracks):
            enriched.append(
                {
                    "videoId": track["videoId"],
                    "title": track.get("title", ""),
                    "artist": track_artist(track),
                    "genre": genre_lookup.genre_for(track_artist(track)) if genre_lookup.enabled else None,
                }
            )
            if (i + 1) % ENRICHMENT_PROGRESS_CHECKPOINT == 0 or i + 1 == len(tracks):
                reorganize_session.enriched_count = i + 1
                reorganize_session_repo.update(reorganize_session)

        existing_context = _format_existing_playlists_context(
            playlist_repo.list_for_user(reorganize_session.user_id)
        )

        circuit_breaker = CircuitBreaker()
        for suggestion in _cluster_and_name(
            circuit_breaker, vector_repo, reorganize_session.user_id, enriched, existing_context
        ):
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


class OnboardingProposalsAlreadyInProgressError(Exception):
    """Raised by trigger_propose_new_playlists when this user's own
    proposals run is already in progress -- mirrors
    IngestionAlreadyInProgressError / MatchingAlreadyInProgressError so a
    rapid page remount can't get two concurrent runs past this synchronous
    pre-flight check."""


def trigger_propose_new_playlists(user_repository: UserRepository, user_id: int) -> User:
    """Synchronous pre-flight for the onboarding-proposals trigger endpoint:
    claims the concurrent-run guard before the background task starts,
    mirroring reorganize_matching.trigger_matching / ingestion.trigger_ingestion_check."""
    user = user_repository.get(user_id)
    if user.proposals_status == "in_progress":
        raise OnboardingProposalsAlreadyInProgressError(
            f"onboarding proposals generation for user {user_id} already in progress"
        )
    return user_repository.set_proposals_progress(user_id, status="in_progress", processed=0, total=0)


def run_propose_new_playlists(
    user_id: int, liked_songs: list[dict], engine=None, vector_repo=None, openrouter_api_key=None
) -> None:
    """Background task (mirrors run_reorganize_clustering's pattern):
    generates Onboarding's initial AI-suggested new playlists from the
    user's complete current liked-songs library, fetched fresh by the
    trigger endpoint the same way Reorganize's does (not scoped to
    whatever's already in LibraryItem) -- a brand-new user has nothing in
    LibraryItem yet (ingestion is gated on onboarding completing, see this
    module's docstring), so a DB-scoped read here would have proposed
    nothing at all for exactly the users onboarding exists for. Same
    progress checkpointing as Reorganize's clustering, persisting proposals
    incrementally via OnboardingProposalRepository.merge_proposal so the
    poll endpoint has something durable to read as soon as it's ready,
    instead of blocking on the whole library like the old synchronous call
    did.

    `engine`/`vector_repo` follow the same injectable-for-tests pattern as
    run_reorganize_clustering. `openrouter_api_key` similarly -- leaving it
    unset (None) reads the real key from Settings; tests pass an explicit
    value (including "") to control the no-key-configured branch
    deterministically.
    """
    engine = engine or get_engine()
    vector_repo = vector_repo or get_vector_repository()
    if openrouter_api_key is None:
        openrouter_api_key = get_settings().openrouter_api_key
    with Session(engine) as db_session:
        user_repository = UserRepository(db_session)
        playlist_repository = PlaylistRepository(db_session)
        proposal_repository = OnboardingProposalRepository(db_session)
        genre_lookup = GenreLookupService(db_session)

        # Fresh run: a re-trigger (e.g. the library changed since last time)
        # shouldn't leave stale suggestions from the previous snapshot mixed
        # in with new ones.
        proposal_repository.clear_for_user(user_id)
        user_repository.set_proposals_progress(user_id, processed=0, total=0)

        if not openrouter_api_key:
            user_repository.set_proposals_progress(user_id, status="done")
            return

        tracks = [song for song in liked_songs if song.get("videoId")]
        if not tracks:
            user_repository.set_proposals_progress(user_id, status="done")
            return

        # Genre lookup, mostly cache hits already -- checkpointed the same
        # way the old BPM phase was, though it rarely needs more than one
        # flush now that genre is the only enrichment step.
        enriched = []
        for i, track in enumerate(tracks):
            enriched.append(
                {
                    "videoId": track["videoId"],
                    "title": track.get("title", ""),
                    "artist": track_artist(track),
                    "genre": genre_lookup.genre_for(track_artist(track)) if genre_lookup.enabled else None,
                }
            )
            if (i + 1) % ENRICHMENT_PROGRESS_CHECKPOINT == 0 or i + 1 == len(tracks):
                user_repository.set_proposals_progress(user_id, processed=i + 1, total=len(tracks))

        existing_context = _format_existing_playlists_context(
            playlist_repository.list_for_user(user_id)
        )

        circuit_breaker = CircuitBreaker()
        proposals_created = 0
        for suggestion in _cluster_and_name(
            circuit_breaker, vector_repo, user_id, enriched, existing_context
        ):
            proposal_repository.merge_proposal(
                user_id, suggestion["name"], suggestion["theme"], suggestion["count"]
            )
            proposals_created += 1

        # R14/KTD2: distinguish a genuinely failed run from one that
        # legitimately found no clusters. llm_clustering is only DEGRADED
        # here if a naming call actually failed (set inside _name_cluster
        # above) -- a healthy run that finds too few/no natural clusters
        # (e.g. below MIN_CLUSTER_SIZE) never calls the LLM at all and
        # leaves this OK, so that legitimate case still ends "done". A run
        # where at least one cluster was successfully named also stays
        # "done" even if a different cluster's naming failed -- only a run
        # with zero proposals created at all is reported as failed.
        llm_status, _ = dependency_health_store.get_status("llm_clustering")
        final_status = (
            "failed" if llm_status == DependencyStatus.DEGRADED and proposals_created == 0 else "done"
        )
        user_repository.set_proposals_progress(user_id, status=final_status)


class PlaylistRemovalRequiresConfirmationError(Exception):
    """Raised by complete_onboarding (U3, KTD7) when unchecking an
    already-tracked playlist would silently orphan non-terminal review work
    (pending/approved_pending_apply items still referencing it). The caller
    must resubmit with this playlist id included in
    `confirmed_removed_playlist_ids` to proceed anyway.
    """

    def __init__(self, playlist_id: int, playlist_name: str, pending_count: int):
        self.playlist_id = playlist_id
        self.playlist_name = playlist_name
        self.pending_count = pending_count
        super().__init__(
            f"playlist {playlist_id} ({playlist_name!r}) has {pending_count} "
            "pending/approved_pending_apply item(s) referencing it -- confirm removal to proceed"
        )


class LibraryAnalysisService:
    def __init__(
        self,
        library_repository: LibraryRepository,
        playlist_repository: PlaylistRepository,
        review_queue_repository: ReviewQueueRepository,
        user_repository: UserRepository,
        music_client: MusicServiceClient,
        reorganize_session_repository: Optional[ReorganizeSessionRepository] = None,
        onboarding_proposal_repository: Optional[OnboardingProposalRepository] = None,
        vector_repository: Optional[QdrantVectorRepository] = None,
    ):
        self.library_repository = library_repository
        self.playlist_repository = playlist_repository
        self.review_queue_repository = review_queue_repository
        self.user_repository = user_repository
        self.music_client = music_client
        self.reorganize_session_repository = reorganize_session_repository
        self.onboarding_proposal_repository = onboarding_proposal_repository
        self.vector_repository = vector_repository or get_vector_repository()

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
            enriched_count=reorganize_session.enriched_count,
            total_count=len(reorganize_session.video_id_snapshot),
            matching_status=reorganize_session.matching_status,
            matched_count=reorganize_session.matched_count,
        )

    def cancel_reorganize(self, reorganize_session_id: int, user_id: int) -> ReorganizeSession:
        """Lets the user abandon an open session instead of being forced to
        finish it. Rejects every non-terminal session-tagged
        review_queue_item -- those decisions (and any unclustered remainder
        of the snapshot) are simply discarded, never written to YouTube --
        then marks the session cancelled so a later trigger starts a fresh
        one rather than resuming this one (see _is_unresolved).
        """
        reorganize_session = self.reorganize_session_repository.get(reorganize_session_id)
        if reorganize_session is None or reorganize_session.user_id != user_id:
            raise ReorganizeSessionNotFoundError(f"reorganize session {reorganize_session_id} not found")
        if reorganize_session.apply_status == "in_progress":
            raise ApplyAlreadyInProgressError(
                f"reorganize session {reorganize_session_id} has an apply in progress"
            )

        for item in self.review_queue_repository.list_for_user(user_id):
            if item.reorganize_session_id != reorganize_session_id:
                continue
            if item.status not in ("pending", "approved_pending_apply"):
                continue
            try:
                self.review_queue_repository.update(item.id, item.version, status="rejected")
            except VersionConflictError:
                continue  # a concurrent update already resolved this row

        reorganize_session.clustering_status = "cancelled"
        return self.reorganize_session_repository.update(reorganize_session)

    def get_proposals_status(self, user_id: int) -> "OnboardingProposalsStatus":
        """Poll-endpoint read for onboarding's initial AI-suggested
        playlists (mirrors get_reorganize_status): reports whatever's been
        persisted so far by the background run plus its enrichment
        progress. Visibility (MIN_CLUSTER_SIZE) is applied here, not at
        write time, so a cluster that only crosses the threshold once two
        same-named clusters merge isn't lost in between (KTD8-style)."""
        user = self.user_repository.get(user_id)
        proposals = [
            p
            for p in self.onboarding_proposal_repository.list_for_user(user_id)
            if p.song_count >= MIN_CLUSTER_SIZE
        ]
        return OnboardingProposalsStatus(
            proposals_status=user.proposals_status,
            proposals_processed_count=user.proposals_processed_count,
            proposals_total_count=user.proposals_total_count,
            proposals=proposals,
        )

    def complete_onboarding(
        self,
        user_id: int,
        accepted_proposals: list[dict],
        custom_playlists: list[dict],
        adopted_playlists: Optional[list[dict]] = None,
        removed_playlist_ids: Optional[list[int]] = None,
        confirmed_removed_playlist_ids: Optional[list[int]] = None,
    ) -> list[Playlist]:
        """Creates the local playlist record per accepted proposal and per
        custom addition (F5 step 3) — never attaches songs, never creates
        review_queue items.

        Suggested/custom playlists get a real YouTube-side create
        immediately UNLESS the user has an open reorganize session (KTD6):
        during Reorganize, the local `Playlist` row is created right away
        with `youtube_playlist_id=None` so session-scoped matching (U4) can
        run against it, but the actual YouTube playlist isn't created until
        Finish & Apply (U6) -- avoiding creating YouTube playlists for
        selections the user might still back out of before applying.

        `adopted_playlists` are playlists that already exist on YouTube
        Music (surfaced by `list_existing_youtube_playlists`) that the user
        chose to bring under this app's management — no YouTube-side create,
        just a local record linked to the existing `playlist_id` so
        classification (`run_ingestion_check`'s `candidates`) and manual
        approve/move can target it like any app-created playlist.

        `removed_playlist_ids` are the reverse: already-tracked playlists the
        user unchecked, meaning "stop managing this one." Blocked (KTD7,
        `PlaylistRemovalRequiresConfirmationError`) whenever any
        pending/approved_pending_apply review_queue_item still references
        it, unless its id is also present in `confirmed_removed_playlist_ids`
        -- unlike onboarding's original invariant (no review work could
        exist yet), Reorganize can easily have live decisions pending
        against a playlist the user is now unchecking. Once removal
        proceeds, only the local record and its dangling review_queue
        references are cleared -- the real playlist and its songs on
        YouTube are never touched. Any review_queue_item still pointing at
        it (matched or manually assigned, at any status) has its
        playlist_id reset to None rather than left dangling, since the
        local Playlist row it named is gone.

        Marks onboarding complete so U4's backfill is allowed to start.
        """
        confirmed_ids = set(confirmed_removed_playlist_ids or [])
        removals = []
        for playlist_id in removed_playlist_ids or []:
            playlist = self.playlist_repository.get(playlist_id)
            if playlist is None or playlist.user_id != user_id:
                continue
            if playlist_id not in confirmed_ids:
                pending_count = self.review_queue_repository.count_non_terminal_references(
                    playlist_id
                )
                if pending_count > 0:
                    raise PlaylistRemovalRequiresConfirmationError(
                        playlist_id, playlist.name, pending_count
                    )
            removals.append(playlist)

        for playlist in removals:
            self.review_queue_repository.clear_playlist_references(playlist.id)
            self.playlist_repository.delete(playlist)

        # KTD6: an open reorganize session defers the actual YouTube create
        # for suggested/custom selections to Finish & Apply (U6).
        has_open_session = (
            self.reorganize_session_repository is not None
            and self.reorganize_session_repository.get_open_for_user(user_id) is not None
        )

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
                        source="adopted",
                    )
                )
            )
        for proposal in accepted_proposals:
            name = proposal["name"]
            description = proposal.get("theme")
            youtube_playlist_id = (
                None if has_open_session else self.music_client.create_playlist(name, description or "")
            )
            created.append(
                self.playlist_repository.create(
                    Playlist(
                        user_id=user_id,
                        name=name,
                        description=description,
                        rule=None,
                        youtube_playlist_id=youtube_playlist_id,
                        source="proposal",
                    )
                )
            )
        for custom in custom_playlists:
            name = custom["name"]
            description = custom.get("description")
            youtube_playlist_id = (
                None if has_open_session else self.music_client.create_playlist(name, description or "")
            )
            created.append(
                self.playlist_repository.create(
                    Playlist(
                        user_id=user_id,
                        name=name,
                        description=description,
                        rule=None,
                        youtube_playlist_id=youtube_playlist_id,
                        source="custom",
                    )
                )
            )

        self.user_repository.mark_onboarding_completed(user_id)
        return created
