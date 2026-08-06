"""On-demand ingestion check (U4, F1, R10) — no background scheduler (KTD5)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import (
    IngestionDependencies,
    get_classification_service,
    get_default_user,
    get_ingestion_dependencies,
    get_music_client,
)
from app.integrations.base import MusicServiceClient
from app.jobs.ingestion import reset_backlog, run_ingestion_check
from app.models.user import User
from app.services.classification import ClassificationService

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


class IngestionCheckResponse(BaseModel):
    ran: bool
    mode: str
    new_songs_found: int
    queue_items_created: int
    songs_marked_removed: int
    backfill_complete: bool


class BacklogResetResponse(BaseModel):
    library_items_cleared: int


@router.post("/check", response_model=IngestionCheckResponse)
def check(
    user: User = Depends(get_default_user),
    music_client: MusicServiceClient = Depends(get_music_client),
    classification_service: ClassificationService = Depends(get_classification_service),
    deps: IngestionDependencies = Depends(get_ingestion_dependencies),
) -> IngestionCheckResponse:
    result = run_ingestion_check(
        music_client=music_client,
        classification_service=classification_service,
        library_repository=deps.library_repository,
        playlist_repository=deps.playlist_repository,
        review_queue_repository=deps.review_queue_repository,
        user_repository=deps.user_repository,
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


@router.post("/reset", response_model=BacklogResetResponse)
def reset(
    user: User = Depends(get_default_user),
    deps: IngestionDependencies = Depends(get_ingestion_dependencies),
) -> BacklogResetResponse:
    result = reset_backlog(
        library_repository=deps.library_repository,
        review_queue_repository=deps.review_queue_repository,
        user_repository=deps.user_repository,
        user_id=user.id,
    )
    return BacklogResetResponse(library_items_cleared=result.library_items_cleared)
