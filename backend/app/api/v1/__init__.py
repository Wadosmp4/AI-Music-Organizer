from fastapi import APIRouter

from app.api.v1.auth_status import router as auth_status_router
from app.api.v1.auth_youtube import router as auth_youtube_router
from app.api.v1.ingestion import router as ingestion_router
from app.api.v1.onboarding import router as onboarding_router
from app.api.v1.playlists import router as playlists_router
from app.api.v1.review_queue import router as review_queue_router
from app.core.config import get_settings

router = APIRouter(prefix=get_settings().api_v1_prefix)
router.include_router(review_queue_router)
router.include_router(playlists_router)
router.include_router(onboarding_router)
router.include_router(ingestion_router)
router.include_router(auth_status_router)
router.include_router(auth_youtube_router)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
