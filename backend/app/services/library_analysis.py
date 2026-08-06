"""Onboarding library analysis and new-playlist selection (U9, F5, R7).

Distinct from U3's per-song classification hot path: this batches the
*entire* unplaced backlog through the LLM at once (ported from
organize_music.py's suggest_playlists) to find clusters of thematically
related songs worth proposing as brand-new playlists — name, theme, and an
estimated song count only. No songs are attached and no review_queue items
are created here (R8) — that's U4's backfill, which this unit deliberately
gates: only after the user's selection completes does onboarding_completed_at
get set, which U4 checks before it starts classifying (F5 step 4).
"""

from dataclasses import dataclass
from typing import Optional

import litellm
from litellm import completion
from pydantic import BaseModel

from app.integrations.base import MusicServiceClient
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.integrations.http_client import CircuitBreaker, call_with_retry
from app.models.playlist import Playlist
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.services.genre_lookup import GenreLookupService
from app.services.unplaced import unplaced_library_items

litellm.suppress_debug_info = True

CLUSTERING_MODEL = "openrouter/google/gemini-2.5-flash"
BATCH_SIZE = 150  # a single call over thousands of tracks overflows the model's output budget
MIN_CLUSTER_SIZE = 4  # ported from organize_music.py — minimum songs to justify a new playlist

SYSTEM_PROMPT = """
You are organizing a YouTube Music library into playlists.

You will get a numbered list of songs (artist, title, and a rough genre tag when
known). Group them into a small number of thematically coherent playlists using
your own knowledge of these artists/songs — genre, mood, era, or language. The
genre tag is only a hint; it may be missing or wrong.

Rules:
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


def _format_tracks(tracks: list[dict]) -> str:
    lines = []
    for i, t in enumerate(tracks):
        genre = f" [{t['genre']}]" if t.get("genre") else ""
        lines.append(f"{i}: {t['artist']} - {t['title']}{genre}")
    return "\n".join(lines)


class LibraryAnalysisService:
    def __init__(
        self,
        library_repository: LibraryRepository,
        playlist_repository: PlaylistRepository,
        review_queue_repository: ReviewQueueRepository,
        user_repository: UserRepository,
        music_client: MusicServiceClient,
        genre_lookup: Optional[GenreLookupService] = None,
        openrouter_api_key: Optional[str] = None,
    ):
        self.library_repository = library_repository
        self.playlist_repository = playlist_repository
        self.review_queue_repository = review_queue_repository
        self.user_repository = user_repository
        self.music_client = music_client
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

    def propose_new_playlists(self, user_id: int) -> list[PlaylistProposal]:
        if not self.openrouter_api_key:
            return []

        unplaced = unplaced_library_items(
            user_id, self.library_repository, self.review_queue_repository
        )
        if not unplaced:
            return []

        enriched = [
            {
                "videoId": item.video_id,
                "title": item.title,
                "artist": item.artist,
                "genre": self.genre_lookup.genre_for(item.artist) if self.genre_lookup else None,
            }
            for item in unplaced
        ]

        merged: dict[str, dict] = {}
        for i in range(0, len(enriched), BATCH_SIZE):
            batch = enriched[i : i + BATCH_SIZE]
            for suggestion in self._cluster_batch(batch):
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

    def _cluster_batch(self, batch: list[dict]) -> list[dict]:
        schema = _PlaylistSuggestions.model_json_schema()

        def _call():
            return completion(
                model=CLUSTERING_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _format_tracks(batch)},
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
                circuit_breaker=self._circuit_breaker,
            )
            raw = resp.choices[0].message.content or ""
            parsed = _PlaylistSuggestions.model_validate_json(raw.strip())
        except Exception as exc:
            # Caught broadly and deliberately, matching the sibling LLM call
            # sites in bpm_lookup.py/classification.py (KTD18): an empty
            # `choices` list (e.g. a safety-filtered response) raises IndexError
            # on `resp.choices[0]`, which the previous narrow except tuple
            # didn't cover. A batch-level clustering failure just leaves those
            # songs unclustered for this pass — never blocks the rest of the
            # run. Own health-store key (distinct from BPM-estimate/
            # description-match, KTD17) so one LLM use case's failure can't
            # mask another's.
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

    def complete_onboarding(
        self,
        user_id: int,
        accepted_proposals: list[dict],
        custom_playlists: list[dict],
    ) -> list[Playlist]:
        """Creates the real YouTube Music playlist and its empty local record
        per accepted proposal and per custom addition (F5 step 3) — never
        attaches songs, never creates review_queue items. A record without a
        `youtube_playlist_id` could never be approved/moved into later, so
        the YouTube-side create happens here, not deferred to first approve.
        Marks onboarding complete so U4's backfill is allowed to start.
        """
        created = []
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
