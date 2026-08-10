from typing import Optional

from sqlalchemy.exc import IntegrityError
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
        if entry is not None:
            entry.genre = genre
            return save(self.session, entry)

        try:
            return save(self.session, GenreCacheEntry(artist=artist, genre=genre))
        except IntegrityError:
            # A concurrent classify_track call for the same not-yet-cached
            # artist (classify_tracks_concurrently fans per-song
            # classification out across a thread pool, each worker with its
            # own DB session) already inserted this artist between this
            # call's get() and this insert. Roll back the failed insert and
            # return the concurrent writer's row instead of raising and
            # losing this song's whole classification result over a cache
            # race (KTD18-style: one thing's conflict shouldn't cost real
            # work already done).
            self.session.rollback()
            existing = self.get(artist)
            if existing is not None:
                return existing
            raise  # genuinely unexpected -- the row should exist now
