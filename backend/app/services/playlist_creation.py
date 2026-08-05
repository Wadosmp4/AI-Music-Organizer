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

from app.integrations.base import Track
from app.models.library import LibraryItem
from app.models.playlist import Playlist
from app.models.review_queue import ReviewQueueItem
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.classification import CandidatePlaylist, ClassificationService


@dataclass
class PlaylistCreationResult:
    playlist: Playlist
    review_queue_items_created: int


def _track_from_library_item(item: LibraryItem) -> Track:
    return {"videoId": item.video_id, "title": item.title, "artists": [{"name": item.artist}]}


class PlaylistCreationService:
    def __init__(
        self,
        playlist_repository: PlaylistRepository,
        library_repository: LibraryRepository,
        review_queue_repository: ReviewQueueRepository,
        classification_service: ClassificationService,
    ):
        self.playlist_repository = playlist_repository
        self.library_repository = library_repository
        self.review_queue_repository = review_queue_repository
        self.classification_service = classification_service

    def create_playlist_and_propose_matches(
        self, user_id: int, name: str, description: str
    ) -> PlaylistCreationResult:
        """Creates the playlist (with its description persisted, KTD11) and
        immediately proposes matches against the current library — never
        auto-applied (R8/R11), only pending review_queue rows.
        """
        playlist = self.playlist_repository.create(
            Playlist(user_id=user_id, name=name, description=description, rule=None)
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
        library_items = self.library_repository.list_for_user(user_id)

        created_count = 0
        for item in library_items:
            track = _track_from_library_item(item)
            result = self.classification_service.classify_track(track, candidates, user_id=user_id)
            if result.playlist_id != playlist.id:
                continue

            self.review_queue_repository.create(
                ReviewQueueItem(
                    user_id=user_id,
                    library_item_id=item.id,
                    playlist_id=playlist.id,
                    confidence=result.confidence,
                    explanation={
                        "signal": result.explanation.signal,
                        "detail": result.explanation.detail,
                        "bpm": result.bpm,
                        "bpm_source": result.bpm_source,
                        "genre": result.genre,
                    },
                )
            )
            created_count += 1
        return created_count

    def _build_candidates(self, user_id: int, playlist: Playlist) -> list[CandidatePlaylist]:
        """The classify_track candidate set: this playlist plus the user's other
        playlists, so an existing playlist's rule (KTD10 hard gate) still takes
        precedence over this one's description where it should. Artist-count
        tracking for *other* pre-existing playlists' contents isn't wired up by
        this unit — that's a future ingestion-tick concern — so they're passed
        with an empty artist_counts; a brand-new playlist has no existing songs
        either way, so its own artist_counts is always empty.
        """
        candidates = [
            CandidatePlaylist(
                id=existing.id,
                name=existing.name,
                rule=existing.rule,
                description=existing.description,
                artist_counts={},
            )
            for existing in self.playlist_repository.list_for_user(user_id)
            if existing.id != playlist.id
        ]
        candidates.append(
            CandidatePlaylist(
                id=playlist.id,
                name=playlist.name,
                rule=playlist.rule,
                description=playlist.description,
                artist_counts={},
            )
        )
        return candidates
