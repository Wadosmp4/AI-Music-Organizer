from fastapi import APIRouter

from app.api.v1.review_queue import router as review_queue_router
from app.core.config import get_settings

router = APIRouter(prefix=get_settings().api_v1_prefix)
router.include_router(review_queue_router)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
