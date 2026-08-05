from typing import Optional

from sqlmodel import Session, select

from app.models.playlist import Playlist
from app.repositories.base import save


class PlaylistRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, playlist_id: int) -> Optional[Playlist]:
        return self.session.get(Playlist, playlist_id)

    def create(self, playlist: Playlist) -> Playlist:
        return save(self.session, playlist)

    def list_for_user(self, user_id: int) -> list[Playlist]:
        return list(
            self.session.exec(select(Playlist).where(Playlist.user_id == user_id))
        )
