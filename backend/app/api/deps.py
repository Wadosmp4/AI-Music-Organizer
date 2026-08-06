"""FastAPI dependency providers.

This is a personal, single-user tool — there's no multi-tenant auth in scope,
so every request acts on behalf of one fixed account (DEFAULT_USER_ID) rather
than an authenticated principal.
"""

from dataclasses import dataclass

from fastapi import Depends
from sqlmodel import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.integrations.base import MusicServiceClient
from app.integrations.youtube_data_api_client import YouTubeDataApiClient
from app.models.user import User
from app.repositories.correction_log_repository import CorrectionLogRepository
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.services.bpm_lookup import BpmLookupService
from app.services.classification import ClassificationService
from app.services.genre_lookup import GenreLookupService
from app.services.library_analysis import LibraryAnalysisService
from app.services.playlist_creation import PlaylistCreationService
from app.services.review_queue import ReviewQueueService

# KTD2: single default user_id row — no auth infrastructure in Phase 1.
DEFAULT_USER_ID = 1


def get_default_user(session: Session = Depends(get_session)) -> User:
    """Gets or creates the single personal-use account (DEFAULT_USER_ID) that
    every request operates as, so FK-constrained rows (playlists, library
    items, review_queue items) always have a real `user` row to point at.
    """
    repository = UserRepository(session)
    user = repository.get(DEFAULT_USER_ID)
    if user is None:
        user = repository.create(User(id=DEFAULT_USER_ID, display_name="Default User"))
    return user


def get_music_client() -> MusicServiceClient:
    return YouTubeDataApiClient()


def get_library_repository(session: Session = Depends(get_session)) -> LibraryRepository:
    return LibraryRepository(session)


def get_playlist_repository(session: Session = Depends(get_session)) -> PlaylistRepository:
    return PlaylistRepository(session)


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


def get_classification_service(session: Session = Depends(get_session)) -> ClassificationService:
    genre_lookup = GenreLookupService(session)
    bpm_lookup = BpmLookupService()
    # Wired in so callers that pass user_id benefit from U7's correction
    # feedback; harmless to callers that don't (defaults to no boost).
    return ClassificationService(
        genre_lookup, bpm_lookup, correction_log_repo=CorrectionLogRepository(session)
    )


def get_playlist_creation_service(
    session: Session = Depends(get_session),
    classification_service: ClassificationService = Depends(get_classification_service),
    music_client: MusicServiceClient = Depends(get_music_client),
) -> PlaylistCreationService:
    return PlaylistCreationService(
        playlist_repository=PlaylistRepository(session),
        library_repository=LibraryRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        classification_service=classification_service,
        music_client=music_client,
    )


def get_library_analysis_service(
    session: Session = Depends(get_session),
    music_client: MusicServiceClient = Depends(get_music_client),
) -> LibraryAnalysisService:
    return LibraryAnalysisService(
        library_repository=LibraryRepository(session),
        playlist_repository=PlaylistRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        user_repository=UserRepository(session),
        music_client=music_client,
        genre_lookup=GenreLookupService(session),
        openrouter_api_key=get_settings().openrouter_api_key,
    )


@dataclass
class IngestionDependencies:
    """Bundles the repositories `run_ingestion_check` needs — it's a plain
    function, not a service class, but still gets its repository wiring from
    this one factory like every other route, rather than constructing them
    inline."""

    library_repository: LibraryRepository
    playlist_repository: PlaylistRepository
    review_queue_repository: ReviewQueueRepository
    user_repository: UserRepository


def get_ingestion_dependencies(session: Session = Depends(get_session)) -> IngestionDependencies:
    return IngestionDependencies(
        library_repository=LibraryRepository(session),
        playlist_repository=PlaylistRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        user_repository=UserRepository(session),
    )
