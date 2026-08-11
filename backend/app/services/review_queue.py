"""Review queue as a mutable, per-item API service (R11-R15, F3).

Replaces plan.json/apply_plan's full-file replay with per-item
approve/reject/move actions. Approve and move both hold the item in
`write_pending` while calling the write path for exactly one item; on
failure the item returns to `pending` and the write-path health status is
raised (KTD17), rather than being left stuck as if applied.
"""

import logging
from typing import Optional

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.base import MusicServiceClient, QuotaExceededError, track_from_library_item
from app.models.correction_log import CorrectionLogEntry
from app.models.review_queue import ReviewQueueItem
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository, VersionConflictError
from app.services.classification import build_correction_context

logger = logging.getLogger(__name__)


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

    def _complete_pending_write(
        self,
        pending_item: ReviewQueueItem,
        final_status: str,
        write_succeeded: bool,
        **extra_fields,
    ) -> ReviewQueueItem:
        """Finalizes a write_pending item's outcome via CAS on the version
        captured when it entered write_pending. If a concurrent ingestion
        staleness update (jobs/ingestion.py) has already claimed this row in
        the meantime (KTD19), that version is gone and this CAS raises
        VersionConflictError — indistinguishable, at the client, from an
        ordinary optimistic-concurrency conflict, but semantically different:
        the external write's real outcome (recorded in `write_succeeded`) is
        now orphaned, since the row never reached `final_status`. Logged here
        so it's diagnosable, then re-raised — the existing 409 contract for
        VersionConflictError is unchanged.
        """
        try:
            return self.review_queue_repo.update(
                pending_item.id, pending_item.version, status=final_status, **extra_fields
            )
        except VersionConflictError:
            logger.warning(
                "review_queue_item %s: write %s but the completion CAS "
                "(write_pending -> %s) lost a race to a concurrent update "
                "(likely ingestion staleness marking) — the item's final "
                "status does not reflect this write's real outcome.",
                pending_item.id,
                "succeeded" if write_succeeded else "failed",
                final_status,
            )
            raise

    def approve(self, item_id: int, expected_version: int) -> ReviewQueueItem:
        item = self._require_item(item_id)
        self._check_not_stale(item)

        if item.reorganize_session_id is not None:
            # KTD1/KTD4: a session-tagged item defers the write entirely --
            # no youtube_playlist_id presence check, no external call. CAS
            # straight to the real, decided-but-unwritten state; Finish &
            # Apply (U6) is what actually writes it later.
            return self.review_queue_repo.update(
                item_id, expected_version, status="approved_pending_apply"
            )

        # Resolved before the write_pending CAS: a missing youtube_playlist_id
        # is a data problem (the destination playlist was never linked), not a
        # write-path failure — it must raise ItemNotFoundError untouched, not
        # get caught by the except below and misreported as auth_status
        # NEEDS_RECONNECT.
        youtube_playlist_id = self._youtube_playlist_id(item.playlist_id)
        library_item = self.library_repo.get(item.library_item_id)
        pending_item = self.review_queue_repo.update(item_id, expected_version, status="write_pending")

        try:
            self.music_client.add_playlist_items(youtube_playlist_id, [library_item.video_id])
        except QuotaExceededError:
            # Not a credential problem -- reconnecting OAuth won't free up
            # quota, so don't mislabel this as NEEDS_RECONNECT (KTD17). The
            # item still reverts to pending either way, since it genuinely
            # wasn't written.
            self._complete_pending_write(pending_item, "pending", write_succeeded=False)
            raise
        except Exception as exc:
            auth_status_store.set_write_status(
                AuthStatus.NEEDS_RECONNECT, f"approve write failed: {exc}"
            )
            self._complete_pending_write(pending_item, "pending", write_succeeded=False)
            raise

        return self._complete_pending_write(pending_item, "approved", write_succeeded=True)

    def reject(self, item_id: int, expected_version: int) -> ReviewQueueItem:
        return self.review_queue_repo.update(item_id, expected_version, status="rejected")

    def move(self, item_id: int, expected_version: int, new_playlist_id: int) -> ReviewQueueItem:
        item = self._require_item(item_id)
        self._check_not_stale(item)

        old_playlist_id = item.playlist_id

        if item.reorganize_session_id is not None:
            # KTD1/KTD4: same deferred-write branch as approve() -- CAS
            # straight to approved_pending_apply with the new playlist_id,
            # no external call.
            moved_item = self.review_queue_repo.update(
                item_id, expected_version, status="approved_pending_apply", playlist_id=new_playlist_id
            )
        else:
            # Resolved before the write_pending CAS — see the matching comment in
            # approve(): a missing youtube_playlist_id on the destination playlist
            # must raise ItemNotFoundError untouched, not get mislabeled as a
            # write-path auth failure.
            youtube_playlist_id = self._youtube_playlist_id(new_playlist_id)
            library_item = self.library_repo.get(item.library_item_id)
            pending_item = self.review_queue_repo.update(
                item_id, expected_version, status="write_pending", playlist_id=new_playlist_id
            )

            try:
                self.music_client.add_playlist_items(youtube_playlist_id, [library_item.video_id])
            except QuotaExceededError:
                # See approve()'s matching branch -- not an auth problem.
                self._complete_pending_write(
                    pending_item, "pending", write_succeeded=False, playlist_id=old_playlist_id
                )
                raise
            except Exception as exc:
                auth_status_store.set_write_status(
                    AuthStatus.NEEDS_RECONNECT, f"move write failed: {exc}"
                )
                self._complete_pending_write(
                    pending_item, "pending", write_succeeded=False, playlist_id=old_playlist_id
                )
                raise

            moved_item = self._complete_pending_write(pending_item, "moved", write_succeeded=True)

        # KTD5: fires for every correction, in-session or not -- decoupled
        # from whether an external write actually happened, so the reorganize
        # pass most likely to contain the most corrections doesn't silently
        # lose the classification-learning signal.
        library_item = self.library_repo.get(item.library_item_id)
        self.correction_log_repo.create(
            CorrectionLogEntry(
                user_id=item.user_id,
                review_queue_item_id=item.id,
                original_playlist_id=old_playlist_id,
                corrected_playlist_id=new_playlist_id,
                # genre omitted: fetching it here would add a synchronous
                # Last.fm call to a user-facing write action; artist_bucket_key
                # (populated below) is _correction_boosts' primary match key,
                # KTD12's genre-scoped matching is a secondary path only.
                context=build_correction_context(track_from_library_item(library_item), genre=None),
            )
        )
        return moved_item

    def add_to_playlist(
        self, item_id: int, expected_version: int, target_playlist_id: int
    ) -> ReviewQueueItem:
        """A song can belong to more than one playlist (e.g. it fits both
        "Chill" and "Late Night") -- never an eager write. Adding it to a
        playlist only ever queues it as a *pending* candidate there, exactly
        like any algorithmic suggestion: it waits for that playlist's own
        Approve-all, never bypassing the review step (R8/R11).

        - Unassigned (playlist_id is None, e.g. sitting in the Review
          Queue's "Unassigned" group): this is its first placement decision,
          so the existing row is simply reassigned to `target_playlist_id`
          (still "pending" -- no write happens here).
        - Already assigned: the existing row is untouched (its original
          suggestion still stands, still pending Approve-all or already
          approved) -- a second, independent "pending" row is created for
          the same song under `target_playlist_id`, so the two placements
          are approved (and written) completely independently.
        """
        item = self._require_item(item_id)
        if item.playlist_id == target_playlist_id:
            return item
        if item.playlist_id is None:
            return self.review_queue_repo.update(
                item_id, expected_version, playlist_id=target_playlist_id
            )

        existing = self._active_item_for_playlist(item.user_id, item.library_item_id, target_playlist_id)
        if existing is not None:
            return existing
        return self.review_queue_repo.create(
            ReviewQueueItem(
                user_id=item.user_id,
                library_item_id=item.library_item_id,
                playlist_id=target_playlist_id,
                status="pending",
                # KTD4: inherits the session tag from the item it splits
                # from, so a manual multi-playlist add during an open
                # session is deferred like any other session-tagged row.
                reorganize_session_id=item.reorganize_session_id,
                confidence=None,
                explanation={"signal": "manual", "detail": "manually added to this playlist"},
            )
        )

    def _active_item_for_playlist(
        self, user_id: int, library_item_id: int, playlist_id: int
    ) -> Optional[ReviewQueueItem]:
        """Guards add_to_playlist against creating a duplicate pending row
        for a song/playlist pair that's already an active candidate there
        (e.g. the user clicks "Add to X" twice, or X already holds this
        song's original algorithmic suggestion)."""
        active_statuses = {"pending", "write_pending", "approved", "moved", "approved_pending_apply"}
        for candidate in self.review_queue_repo.list_for_user(user_id):
            if (
                candidate.library_item_id == library_item_id
                and candidate.playlist_id == playlist_id
                and candidate.status in active_statuses
            ):
                return candidate
        return None
