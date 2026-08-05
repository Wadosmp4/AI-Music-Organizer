from typing import Optional

from sqlmodel import Session, select

from app.models.review_queue import ReviewQueueItem


class VersionConflictError(Exception):
    """Raised when a compare-and-swap update targets a stale `version` (KTD19)."""


class ReviewQueueRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, item_id: int) -> Optional[ReviewQueueItem]:
        return self.session.get(ReviewQueueItem, item_id)

    def create(self, item: ReviewQueueItem) -> ReviewQueueItem:
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def list_for_user(self, user_id: int) -> list[ReviewQueueItem]:
        return list(
            self.session.exec(
                select(ReviewQueueItem).where(ReviewQueueItem.user_id == user_id)
            )
        )

    def update_status(self, item_id: int, expected_version: int, new_status: str) -> ReviewQueueItem:
        """Compare-and-swap status update. Raises VersionConflictError on a version mismatch (KTD19)."""
        item = self.get(item_id)
        if item is None or item.version != expected_version:
            raise VersionConflictError(
                f"review_queue_item {item_id} version mismatch (expected {expected_version})"
            )
        item.status = new_status
        item.version += 1
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item
