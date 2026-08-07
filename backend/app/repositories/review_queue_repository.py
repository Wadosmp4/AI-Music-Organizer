from typing import Optional

from sqlmodel import Session, select

from app.models.review_queue import ReviewQueueItem
from app.repositories.base import save


class VersionConflictError(Exception):
    """Raised when a compare-and-swap update targets a stale `version` (KTD19)."""


class ReviewQueueRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, item_id: int) -> Optional[ReviewQueueItem]:
        return self.session.get(ReviewQueueItem, item_id)

    def create(self, item: ReviewQueueItem) -> ReviewQueueItem:
        return save(self.session, item)

    def delete(self, item: ReviewQueueItem) -> None:
        self.session.delete(item)
        self.session.commit()

    def clear_playlist_references(self, playlist_id: int) -> None:
        """Un-targets every review_queue_item pointing at a playlist that's
        about to be deleted (onboarding "uncheck to stop managing" -- see
        LibraryAnalysisService.complete_onboarding), so no row is left
        referencing a playlist.id that no longer exists. Not a per-item CAS
        update: this is an admin-level bulk cleanup triggered by the
        playlist's owner, not a concurrent single-item write.

        Callers must check `count_non_terminal_references` first (KTD7) --
        this method itself performs no such guard, since it's also the
        confirmed-removal path once the caller has decided to proceed.
        """
        items = list(
            self.session.exec(select(ReviewQueueItem).where(ReviewQueueItem.playlist_id == playlist_id))
        )
        for item in items:
            item.playlist_id = None
            item.version += 1
            self.session.add(item)
        self.session.commit()

    # Statuses representing real, still-undecided-or-unwritten work against a
    # playlist (KTD7) -- distinct from PLACED_STATUSES/ACTIVE_QUEUE_STATUSES
    # elsewhere, this is specifically "would this uncheck silently orphan
    # work the user hasn't resolved yet."
    _NON_TERMINAL_REFERENCE_STATUSES = ("pending", "approved_pending_apply")

    def count_non_terminal_references(self, playlist_id: int) -> int:
        """KTD7: counts review_queue_items still referencing this playlist
        that aren't yet a finished outcome (pending or
        approved_pending_apply). Used to block unchecking an already-tracked
        playlist during Reorganize pending explicit confirmation -- today's
        unconditional `clear_playlist_references` null-out was only safe
        under onboarding's old invariant that no review work could exist yet.
        """
        items = self.session.exec(
            select(ReviewQueueItem).where(
                ReviewQueueItem.playlist_id == playlist_id,
                ReviewQueueItem.status.in_(self._NON_TERMINAL_REFERENCE_STATUSES),
            )
        )
        return len(list(items))
        self.session.commit()

    def list_for_user(self, user_id: int) -> list[ReviewQueueItem]:
        return list(
            self.session.exec(
                select(ReviewQueueItem).where(ReviewQueueItem.user_id == user_id)
            )
        )

    def update(self, item_id: int, expected_version: int, **fields) -> ReviewQueueItem:
        """Compare-and-swap update of arbitrary fields. Raises VersionConflictError
        on a version mismatch (KTD19) — covers every writer to this row: user
        actions and the ingestion job's staleness marking alike.

        Uses `populate_existing=True` rather than plain `self.get()`: a
        session that already loaded this row earlier (e.g. approve()/move()
        calling update() twice — once into write_pending, once to complete)
        would otherwise return its own stale identity-mapped copy here
        instead of the row's true current state, letting this CAS silently
        "succeed" (and clobber a concurrent writer's committed change) even
        when a different session moved the version out from under it in the
        meantime.
        """
        item = self.session.get(ReviewQueueItem, item_id, populate_existing=True)
        if item is None or item.version != expected_version:
            raise VersionConflictError(
                f"review_queue_item {item_id} version mismatch (expected {expected_version})"
            )
        for key, value in fields.items():
            setattr(item, key, value)
        item.version += 1
        return save(self.session, item)

    def update_status(self, item_id: int, expected_version: int, new_status: str) -> ReviewQueueItem:
        """Compare-and-swap status update. Raises VersionConflictError on a version mismatch (KTD19)."""
        return self.update(item_id, expected_version, status=new_status)
