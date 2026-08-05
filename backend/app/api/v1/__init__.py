from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(prefix=get_settings().api_v1_prefix)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
