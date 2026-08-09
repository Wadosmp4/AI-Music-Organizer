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

    def delete(self, playlist: Playlist) -> None:
        self.session.delete(playlist)
        self.session.commit()

    def update_description(self, playlist_id: int, description: str) -> Optional[Playlist]:
        playlist = self.get(playlist_id)
        if playlist is None:
            return None
        playlist.description = description
        return save(self.session, playlist)

    def set_youtube_playlist_id(self, playlist_id: int, youtube_playlist_id: str) -> Optional[Playlist]:
        """Backfills a placeholder playlist's real id once Finish & Apply
        (U6, KTD6) creates it on YouTube -- until this call, the row exists
        locally with `youtube_playlist_id=None` (set at selection time, U3)."""
        playlist = self.get(playlist_id)
        if playlist is None:
            return None
        playlist.youtube_playlist_id = youtube_playlist_id
        return save(self.session, playlist)

    def list_for_user(self, user_id: int) -> list[Playlist]:
        return list(
            self.session.exec(select(Playlist).where(Playlist.user_id == user_id))
        )
