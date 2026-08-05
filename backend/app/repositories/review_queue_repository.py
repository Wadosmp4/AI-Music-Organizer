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

    def list_for_user(self, user_id: int) -> list[ReviewQueueItem]:
        return list(
            self.session.exec(
                select(ReviewQueueItem).where(ReviewQueueItem.user_id == user_id)
            )
        )

    def update(self, item_id: int, expected_version: int, **fields) -> ReviewQueueItem:
        """Compare-and-swap update of arbitrary fields. Raises VersionConflictError
        on a version mismatch (KTD19) — covers every writer to this row: user
        actions and the ingestion job's staleness marking alike."""
        item = self.get(item_id)
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
