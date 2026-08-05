from fastapi import Depends
from sqlmodel import Session

from app.core.db import get_session
from app.integrations.base import MusicServiceClient
from app.integrations.ytmusic_client import YTMusicClient
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.services.review_queue import ReviewQueueService

# KTD2: single default user_id row — no auth infrastructure in Phase 1.
DEFAULT_USER_ID = 1


def get_music_client() -> MusicServiceClient:
    return YTMusicClient()


def get_review_queue_service(
    session: Session = Depends(get_session),
    music_client: MusicServiceClient = Depends(get_music_client),
) -> ReviewQueueService:
    return ReviewQueueService(
        review_queue_repo=ReviewQueueRepository(session),
        library_repo=LibraryRepository(session),
        playlist_repo=PlaylistRepository(session),
        correction_log_repo=CorrectionLogRepository(session),
        music_client=music_client,
    )
