"""On-demand ingestion check (U4, F1, R10) — no background scheduler (KTD5)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import (
    get_classification_service,
    get_default_user,
    get_music_client,
)
from app.core.db import get_session
from app.integrations.base import MusicServiceClient
from app.jobs.ingestion import run_ingestion_check
from app.models.user import User
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import ReviewQueueRepository
from app.repositories.user_repository import UserRepository
from app.services.classification import ClassificationService

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


class IngestionCheckResponse(BaseModel):
    ran: bool
    mode: str
    new_songs_found: int
    queue_items_created: int
    songs_marked_removed: int
    backfill_complete: bool


@router.post("/check", response_model=IngestionCheckResponse)
def check(
    session=Depends(get_session),
    user: User = Depends(get_default_user),
    music_client: MusicServiceClient = Depends(get_music_client),
    classification_service: ClassificationService = Depends(get_classification_service),
) -> IngestionCheckResponse:
    result = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=LibraryRepository(session),
        playlist_repository=PlaylistRepository(session),
        review_queue_repository=ReviewQueueRepository(session),
        user_repository=UserRepository(session),
        user_id=user.id,
    )
    return IngestionCheckResponse(
        ran=result.ran,
        mode=result.mode,
        new_songs_found=result.new_songs_found,
        queue_items_created=result.queue_items_created,
        songs_marked_removed=result.songs_marked_removed,
        backfill_complete=result.backfill_complete,
    )
