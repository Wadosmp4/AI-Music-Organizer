from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.deps import (
    DEFAULT_USER_ID,
    get_library_repository,
    get_playlist_repository,
    get_review_queue_service,
)
from app.models.review_queue import ReviewQueueItem
from app.repositories.library_repository import LibraryRepository
from app.repositories.playlist_repository import PlaylistRepository
from app.repositories.review_queue_repository import VersionConflictError
from app.services.review_queue import ItemNotFoundError, ReviewQueueService, StaleItemError

# Raised by the service layer and mapped to their HTTP status uniformly by
# the handlers registered in main.py — must propagate past this route's own
# generic except-Exception below, not be swallowed into a 502.
_DOMAIN_EXCEPTIONS = (ItemNotFoundError, StaleItemError, VersionConflictError)

router = APIRouter(prefix="/review-queue", tags=["review-queue"])


class ApproveRequest(BaseModel):
    expected_version: int


class RejectRequest(BaseModel):
    expected_version: int


class MoveRequest(BaseModel):
    expected_version: int
    new_playlist_id: int


class AddToPlaylistRequest(BaseModel):
    expected_version: int
    playlist_id: int


class ReviewQueueItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    library_item_id: int
    playlist_id: Optional[int]
    status: str
    version: int
    confidence: Optional[float]
    explanation: Optional[dict]
    # The song a reviewer is actually judging — without these the review
    # queue is unusable (a reviewer can't tell what "library_item_id 42" is).
    title: str
    artist: str
    # Same reasoning applies to the destination: a bare numeric playlist_id
    # forces the reviewer to remember which id is which playlist by heart.
    playlist_name: Optional[str]


def _to_response(
    item: ReviewQueueItem,
    library_repo: LibraryRepository,
    playlist_repo: PlaylistRepository,
) -> ReviewQueueItemResponse:
    library_item = library_repo.get(item.library_item_id)
    playlist = playlist_repo.get(item.playlist_id) if item.playlist_id is not None else None
    return ReviewQueueItemResponse(
        id=item.id,
        library_item_id=item.library_item_id,
        playlist_id=item.playlist_id,
        status=item.status,
        version=item.version,
        confidence=item.confidence,
        explanation=item.explanation,
        title=library_item.title if library_item else "(unknown song)",
        artist=library_item.artist if library_item else "(unknown artist)",
        playlist_name=playlist.name if playlist else None,
    )


@router.get("", response_model=list[ReviewQueueItemResponse])
def list_review_queue(
    service: ReviewQueueService = Depends(get_review_queue_service),
    library_repo: LibraryRepository = Depends(get_library_repository),
    playlist_repo: PlaylistRepository = Depends(get_playlist_repository),
):
    return [
        _to_response(item, library_repo, playlist_repo)
        for item in service.list_items(DEFAULT_USER_ID)
    ]


@router.post("/{item_id}/approve", response_model=ReviewQueueItemResponse)
def approve(
    item_id: int,
    body: ApproveRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
    library_repo: LibraryRepository = Depends(get_library_repository),
    playlist_repo: PlaylistRepository = Depends(get_playlist_repository),
):
    try:
        return _to_response(
            service.approve(item_id, body.expected_version), library_repo, playlist_repo
        )
    except _DOMAIN_EXCEPTIONS:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"write failed: {exc}")


@router.post("/{item_id}/reject", response_model=ReviewQueueItemResponse)
def reject(
    item_id: int,
    body: RejectRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
    library_repo: LibraryRepository = Depends(get_library_repository),
    playlist_repo: PlaylistRepository = Depends(get_playlist_repository),
):
    try:
        return _to_response(
            service.reject(item_id, body.expected_version), library_repo, playlist_repo
        )
    except _DOMAIN_EXCEPTIONS:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"write failed: {exc}")


@router.post("/{item_id}/move", response_model=ReviewQueueItemResponse)
def move(
    item_id: int,
    body: MoveRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
    library_repo: LibraryRepository = Depends(get_library_repository),
    playlist_repo: PlaylistRepository = Depends(get_playlist_repository),
):
    try:
        return _to_response(
            service.move(item_id, body.expected_version, body.new_playlist_id),
            library_repo,
            playlist_repo,
        )
    except _DOMAIN_EXCEPTIONS:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"write failed: {exc}")


@router.post("/{item_id}/add-to-playlist", response_model=ReviewQueueItemResponse)
def add_to_playlist(
    item_id: int,
    body: AddToPlaylistRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
    library_repo: LibraryRepository = Depends(get_library_repository),
    playlist_repo: PlaylistRepository = Depends(get_playlist_repository),
):
    try:
        return _to_response(
            service.add_to_playlist(item_id, body.expected_version, body.playlist_id),
            library_repo,
            playlist_repo,
        )
    except _DOMAIN_EXCEPTIONS:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"write failed: {exc}")
