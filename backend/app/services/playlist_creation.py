"""Creates a playlist from a natural-language description and proposes initial
matches from the user's library for human review.

R8/R11 ("always review, no auto-apply"): every match this produces becomes a
`ReviewQueueItem` with status="pending" (the model default) — nothing here
ever calls a YouTube-writing integration directly. KTD11: the description is
persisted on the `Playlist` row itself, so a later ingestion tick can re-run
`propose_matches` against the same playlist and pick up newly liked songs
without re-deriving the description.
"""

from dataclasses import dataclass

from app.integrations.base import MusicServiceClient, track_from_library_item
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.classification import CandidatePlaylist, ClassificationService, build_candidate
from app.services.unplaced import unplaced_library_items


@dataclass
class PlaylistCreationResult:
    playlist: Playlist
    review_queue_items_created: int


class PlaylistCreationService:
    def __init__(
        self,
        playlist_repository: PlaylistRepository,
        library_repository: LibraryRepository,
        review_queue_repository: ReviewQueueRepository,
        classification_service: ClassificationService,
        music_client: MusicServiceClient,
    ):
        self.playlist_repository = playlist_repository
        self.library_repository = library_repository
        self.review_queue_repository = review_queue_repository
        self.classification_service = classification_service
        self.music_client = music_client

    def create_playlist_and_propose_matches(
        self, user_id: int, name: str, description: str
    ) -> PlaylistCreationResult:
        """Creates the playlist on YouTube Music first (a review_queue item
        that later points at a playlist with no `youtube_playlist_id` can
        never be approved/moved — the write path requires a real target),
        persists its description (KTD11), then proposes matches against the
        current library — never auto-applied (R8/R11), only pending
        review_queue rows.
        """
        youtube_playlist_id = self.music_client.create_playlist(name, description)
        playlist = self.playlist_repository.create(
            Playlist(
                user_id=user_id,
                name=name,
                description=description,
                rule=None,
                youtube_playlist_id=youtube_playlist_id,
                source="custom",
            )
        )
        created_count = self.propose_matches(user_id, playlist)
        return PlaylistCreationResult(playlist=playlist, review_queue_items_created=created_count)

    def propose_matches(self, user_id: int, playlist: Playlist) -> int:
        """Re-runnable matching pass for an already-persisted playlist (KTD11)
        — used both right after creation and by any later ingestion tick that
        wants to match newly-liked songs against this playlist's persisted
        description, not just at creation time.
        """
        candidates = self._build_candidates(user_id, playlist)
        # Only unplaced songs are candidates — a song already approved/moved
        # elsewhere represents a settled decision and isn't reconsidered just
        # because a new playlist was created (also avoids reclassifying the
        # whole library, not just the backlog, on every playlist creation).
        library_items = unplaced_library_items(
            user_id, self.library_repository, self.review_queue_repository
        )

        created_count = 0
        for item in library_items:
            track = track_from_library_item(item)
            try:
                results = self.classification_service.classify_track(track, candidates, user_id=user_id)
            except Exception:
                # KTD18: one song's failure must not block the rest of this
                # batch, mirroring jobs/ingestion.py's identical guard.
                continue
            # classify_track may match this song to several playlists at
            # once (candidates includes every other playlist too, so a rule
            # or vibe fit elsewhere can coexist) -- this method only ever
            # queues it for the one playlist it's scoped to.
            match = next((r for r in results if r.playlist_id == playlist.id), None)
            if match is None:
                continue

            self.review_queue_repository.create(
                ReviewQueueItem(
                    user_id=user_id,
                    library_item_id=item.id,
                    playlist_id=playlist.id,
                    confidence=match.confidence,
                    explanation=match.as_explanation_dict(),
                )
            )
            created_count += 1
        return created_count

    def _build_candidates(self, user_id: int, playlist: Playlist) -> list[CandidatePlaylist]:
        """The classify_track candidate set: this playlist plus the user's other
        playlists, so an existing playlist's rule (KTD10 hard gate) still takes
        precedence over this one's description where it should. Each
        playlist's artist_counts reflects its actual current YouTube content,
        and a missing description is backfilled from that same content
        (build_candidate); the brand-new `playlist` itself has nothing on
        YouTube yet, so its own artist_counts is always empty at this point.
        """
        candidates = [
            build_candidate(self.music_client, self.classification_service, self.playlist_repository, existing)
            for existing in self.playlist_repository.list_for_user(user_id)
            if existing.id != playlist.id
        ]
        candidates.append(
            build_candidate(self.music_client, self.classification_service, self.playlist_repository, playlist)
        )
        return candidates
