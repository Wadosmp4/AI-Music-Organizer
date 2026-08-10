"""System-level ingestion/auth health status (U8, KTD6, KTD17, KTD18).

Two independently surfaced states (write path / detection path, KTD17),
plus the classification-hot-path dependencies (KTD18) — never blended into
one signal, so the UI can point the user at the right reconnect flow
instead of a single ambiguous "something's wrong" banner.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.integrations.auth_status import auth_status_store
from app.integrations.dependency_health import dependency_health_store

router = APIRouter(prefix="/auth-status", tags=["auth-status"])


class StatusResponse(BaseModel):
    status: str
    reason: str | None


class AuthStatusResponse(BaseModel):
    write_path: StatusResponse
    detection_path: StatusResponse
    # Set by the ingestion check (jobs/ingestion.py) whenever the last
    # get_liked_songs() call failed for any reason — a superset of
    # detection_path's OAuth-token-specific failures, also covering plain
    # network errors during that call.
    youtube_detection: StatusResponse
    # Two independent LLM use cases (KTD17/18: never blend independently-
    # surfaced health signals) — a persistent failure in one (e.g. clustering)
    # must not be masked by another (e.g. description-match) succeeding right
    # after it against the same shared health-store key.
    llm_description_match: StatusResponse
    llm_clustering: StatusResponse
    lastfm: StatusResponse


@router.get("", response_model=AuthStatusResponse)
def get_auth_status() -> AuthStatusResponse:
    write_status, write_reason = auth_status_store.get_write_status()
    detection_status, detection_reason = auth_status_store.get_detection_status()
    youtube_detection_status, youtube_detection_reason = dependency_health_store.get_status(
        "youtube_detection"
    )
    llm_desc_status, llm_desc_reason = dependency_health_store.get_status("llm_description_match")
    llm_cluster_status, llm_cluster_reason = dependency_health_store.get_status("llm_clustering")
    lastfm_status, lastfm_reason = dependency_health_store.get_status("lastfm")

    return AuthStatusResponse(
        write_path=StatusResponse(status=write_status.value, reason=write_reason),
        detection_path=StatusResponse(status=detection_status.value, reason=detection_reason),
        youtube_detection=StatusResponse(
            status=youtube_detection_status.value, reason=youtube_detection_reason
        ),
        llm_description_match=StatusResponse(status=llm_desc_status.value, reason=llm_desc_reason),
        llm_clustering=StatusResponse(status=llm_cluster_status.value, reason=llm_cluster_reason),
        lastfm=StatusResponse(status=lastfm_status.value, reason=lastfm_reason),
    )
