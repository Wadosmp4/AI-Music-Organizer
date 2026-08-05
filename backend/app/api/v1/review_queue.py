from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.deps import DEFAULT_USER_ID, get_review_queue_service
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


class ReviewQueueItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    library_item_id: int
    playlist_id: Optional[int]
    status: str
    version: int
    confidence: Optional[float]
    explanation: Optional[dict]


@router.get("", response_model=list[ReviewQueueItemResponse])
def list_review_queue(service: ReviewQueueService = Depends(get_review_queue_service)):
    return service.list_items(DEFAULT_USER_ID)


@router.post("/{item_id}/approve", response_model=ReviewQueueItemResponse)
def approve(
    item_id: int,
    body: ApproveRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
):
    try:
        return service.approve(item_id, body.expected_version)
    except _DOMAIN_EXCEPTIONS:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"write failed: {exc}")


@router.post("/{item_id}/reject", response_model=ReviewQueueItemResponse)
def reject(
    item_id: int,
    body: RejectRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
):
    return service.reject(item_id, body.expected_version)


@router.post("/{item_id}/move", response_model=ReviewQueueItemResponse)
def move(
    item_id: int,
    body: MoveRequest,
    service: ReviewQueueService = Depends(get_review_queue_service),
):
    try:
        return service.move(item_id, body.expected_version, body.new_playlist_id)
    except _DOMAIN_EXCEPTIONS:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"write failed: {exc}")
