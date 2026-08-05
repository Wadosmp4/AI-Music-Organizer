from typing import Optional

from sqlmodel import Session, select

from app.models.library import LibraryItem


class LibraryRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, item_id: int) -> Optional[LibraryItem]:
        return self.session.get(LibraryItem, item_id)

    def get_by_video_id(self, video_id: str) -> Optional[LibraryItem]:
        return self.session.exec(
            select(LibraryItem).where(LibraryItem.video_id == video_id)
        ).first()

    def create(self, item: LibraryItem) -> LibraryItem:
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def list_for_user(self, user_id: int) -> list[LibraryItem]:
        return list(
            self.session.exec(select(LibraryItem).where(LibraryItem.user_id == user_id))
        )
