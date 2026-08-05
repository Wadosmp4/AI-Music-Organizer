"""Review queue as a mutable, per-item API service (R11-R15, F3).

Replaces plan.json/apply_plan's full-file replay with per-item
approve/reject/move actions. Approve and move both hold the item in
`write_pending` while calling the write path for exactly one item; on
failure the item returns to `pending` and the write-path health status is
raised (KTD17), rather than being left stuck as if applied.
"""

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import MusicServiceClient
from app.models.correction_log import CorrectionLogEntry
from app.models.review_queue import ReviewQueueItem
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError


class StaleItemError(Exception):
    """The source song is no longer liked (KTD14) — surfaced instead of approved/moved."""


class ItemNotFoundError(Exception):
    pass


class ReviewQueueService:
    def __init__(
        self,
        review_queue_repo: ReviewQueueRepository,
        library_repo: LibraryRepository,
        playlist_repo: PlaylistRepository,
        correction_log_repo: CorrectionLogRepository,
        music_client: MusicServiceClient,
    ):
        self.review_queue_repo = review_queue_repo
        self.library_repo = library_repo
        self.playlist_repo = playlist_repo
        self.correction_log_repo = correction_log_repo
        self.music_client = music_client

    def list_items(self, user_id: int) -> list[ReviewQueueItem]:
        return self.review_queue_repo.list_for_user(user_id)

    def _require_item(self, item_id: int) -> ReviewQueueItem:
        item = self.review_queue_repo.get(item_id)
        if item is None:
            raise ItemNotFoundError(f"review_queue_item {item_id} not found")
        return item

    def _check_not_stale(self, item: ReviewQueueItem) -> None:
        """Re-validated synchronously at approve/move time (KTD14) — not solely
        relying on the next ingestion tick's staleness pass — so a race between
        a paused scheduler and a user action can't approve a song that's
        already gone."""
        library_item = self.library_repo.get(item.library_item_id)
        if library_item is None or library_item.removed_at is not None:
            self.review_queue_repo.update(item.id, item.version, status="stale")
            raise StaleItemError(f"review_queue_item {item.id}'s source song is no longer liked")

    def _youtube_playlist_id(self, playlist_id: int) -> str:
        playlist = self.playlist_repo.get(playlist_id)
        if playlist is None or not playlist.youtube_playlist_id:
            raise ItemNotFoundError(f"playlist {playlist_id} has no linked YouTube playlist")
        return playlist.youtube_playlist_id

    def approve(self, item_id: int, expected_version: int) -> ReviewQueueItem:
        item = self._require_item(item_id)
        self._check_not_stale(item)

        library_item = self.library_repo.get(item.library_item_id)
        pending_item = self.review_queue_repo.update(item_id, expected_version, status="write_pending")

        try:
            self.music_client.add_playlist_items(
                self._youtube_playlist_id(pending_item.playlist_id), [library_item.video_id]
            )
        except Exception as exc:
            auth_status_store.set_write_status(
                AuthStatus.NEEDS_RECONNECT, f"approve write failed: {exc}"
            )
            self.review_queue_repo.update(pending_item.id, pending_item.version, status="pending")
            raise

        return self.review_queue_repo.update(pending_item.id, pending_item.version, status="approved")

    def reject(self, item_id: int, expected_version: int) -> ReviewQueueItem:
        return self.review_queue_repo.update(item_id, expected_version, status="rejected")

    def move(self, item_id: int, expected_version: int, new_playlist_id: int) -> ReviewQueueItem:
        item = self._require_item(item_id)
        self._check_not_stale(item)

        old_playlist_id = item.playlist_id
        library_item = self.library_repo.get(item.library_item_id)
        pending_item = self.review_queue_repo.update(
            item_id, expected_version, status="write_pending", playlist_id=new_playlist_id
        )

        try:
            self.music_client.add_playlist_items(
                self._youtube_playlist_id(new_playlist_id), [library_item.video_id]
            )
        except Exception as exc:
            auth_status_store.set_write_status(
                AuthStatus.NEEDS_RECONNECT, f"move write failed: {exc}"
            )
            self.review_queue_repo.update(
                pending_item.id, pending_item.version, status="pending", playlist_id=old_playlist_id
            )
            raise

        moved_item = self.review_queue_repo.update(pending_item.id, pending_item.version, status="moved")

        self.correction_log_repo.create(
            CorrectionLogEntry(
                user_id=item.user_id,
                review_queue_item_id=item.id,
                original_playlist_id=old_playlist_id,
                corrected_playlist_id=new_playlist_id,
            )
        )
        return moved_item
