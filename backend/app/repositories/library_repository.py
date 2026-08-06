from typing import Optional

from sqlmodel import Session, select

from app.models.base import utcnow
from app.models.library import LibraryItem
from app.repositories.base import save


class LibraryRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, item_id: int) -> Optional[LibraryItem]:
        return self.session.get(LibraryItem, item_id)

    def mark_removed(self, item_id: int) -> Optional[LibraryItem]:
        """Sets removed_at (KTD14) so approve/move re-checks (U5) and future
        ingestion checks both see this song as no longer liked."""
        item = self.get(item_id)
        if item is None:
            return None
        item.removed_at = utcnow()
        return save(self.session, item)

    def get_by_video_id(self, video_id: str) -> Optional[LibraryItem]:
        return self.session.exec(
            select(LibraryItem).where(LibraryItem.video_id == video_id)
        ).first()

    def create(self, item: LibraryItem) -> LibraryItem:
        return save(self.session, item)

    def delete(self, item: LibraryItem) -> None:
        self.session.delete(item)
        self.session.commit()

    def list_for_user(self, user_id: int) -> list[LibraryItem]:
        return list(
            self.session.exec(select(LibraryItem).where(LibraryItem.user_id == user_id))
        )
