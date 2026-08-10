"""On-demand ingestion check (U4, F1, R10) — no background scheduler (KTD5)."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import (
    IngestionDependencies,
    get_default_user,
    get_ingestion_dependencies,
)
from app.jobs.ingestion import (
    IngestionAlreadyInProgressError,
    reset_backlog,
    run_ingestion_check_to_completion,
    trigger_ingestion_check,
)
from app.models.user import User

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


class IngestionCheckTriggerResponse(BaseModel):
    ran: bool
    mode: str  # "waiting_for_onboarding" | "triggered"
    ingestion_status: str


class IngestionStatusResponse(BaseModel):
    ingestion_status: str
    ingestion_processed_count: int
    ingestion_total_count: int


class BacklogResetResponse(BaseModel):
    library_items_cleared: int


@router.post("/check", response_model=IngestionCheckTriggerResponse)
def check(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    deps: IngestionDependencies = Depends(get_ingestion_dependencies),
) -> IngestionCheckTriggerResponse:
    """Starts a background ingestion-check run (mirrors the reorganize
    matching trigger's shape): loops the bounded batch to completion itself
    -- one click for the user's whole backlog/burst, instead of the old
    "click Load next 50 songs repeatedly" flow -- and persists progress on
    the `user` row for `/ingestion/status` to poll. Still gated on onboarding
    (F5) the same way the old synchronous check was, returned inline rather
    than as an error since it's an expected, common state, not a failure.
    """
    if user.onboarding_completed_at is None:
        return IngestionCheckTriggerResponse(
            ran=False, mode="waiting_for_onboarding", ingestion_status=user.ingestion_status
        )
    try:
        user = trigger_ingestion_check(deps.user_repository, user.id)
    except IngestionAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(run_ingestion_check_to_completion, user.id)
    return IngestionCheckTriggerResponse(
        ran=True, mode="triggered", ingestion_status=user.ingestion_status
    )


@router.get("/status", response_model=IngestionStatusResponse)
def get_ingestion_status(user: User = Depends(get_default_user)) -> IngestionStatusResponse:
    return IngestionStatusResponse(
        ingestion_status=user.ingestion_status,
        ingestion_processed_count=user.ingestion_processed_count,
        ingestion_total_count=user.ingestion_total_count,
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
