from typing import Optional

from sqlmodel import Session, select

from app.models.genre_cache import GenreCacheEntry
from app.repositories.base import save


class GenreCacheRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, artist: str) -> Optional[GenreCacheEntry]:
        return self.session.exec(
            select(GenreCacheEntry).where(GenreCacheEntry.artist == artist)
        ).first()

    def upsert(self, artist: str, genre: Optional[str]) -> GenreCacheEntry:
        entry = self.get(artist)
        if entry is None:
            entry = GenreCacheEntry(artist=artist, genre=genre)
        else:
            entry.genre = genre
        return save(self.session, entry)
