from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import router as api_v1_router
from app.integrations.base import QuotaExceededError
from app.repositories.review_queue_repository import VersionConflictError
from app.services.review_queue import ItemNotFoundError, StaleItemError

app = FastAPI(title="AI Music Organizer")

app.include_router(api_v1_router)

# CSRF guard for a personal single-user tool that has no CORSMiddleware
# configured (KTD2: no multi-tenant auth infrastructure to layer a token
# check onto). The Vite dev server's own origin (KTD26's pinned Docker
# Compose topology) is the only legitimate caller; a mutating request must
# either come from that origin or carry the frontend's custom header —
# a genuine cross-origin fetch() can't add a custom header without a CORS
# preflight, and no CORS policy exists to let that preflight succeed, so a
# plain cross-site form POST (no custom headers, no preflight) is rejected
# outright instead of silently reaching the handler.
_ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}
_CSRF_HEADER = "x-requested-with"
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _reject_cross_origin_mutations(request: Request, call_next):
    if request.method in _MUTATING_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
        if _CSRF_HEADER not in request.headers:
            return JSONResponse(status_code=403, content={"detail": "missing required request header"})
    return await call_next(request)


# Domain-exception-to-HTTP-status mapping, applied uniformly to every route
# rather than repeated per-route try/except (each of these could otherwise be
# raised from more than one review-queue action).
@app.exception_handler(ItemNotFoundError)
def _item_not_found_handler(request: Request, exc: ItemNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(StaleItemError)
def _stale_item_handler(request: Request, exc: StaleItemError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(VersionConflictError)
def _version_conflict_handler(request: Request, exc: VersionConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(QuotaExceededError)
def _quota_exceeded_handler(request: Request, exc: QuotaExceededError) -> JSONResponse:
    # 503 (service temporarily unavailable), not 500 -- this is an external
    # dependency's own rate limit, not a bug, and it self-resolves once the
    # quota window rolls over. A structured `reason` lets the frontend show
    # a specific message instead of a generic error.
    return JSONResponse(
        status_code=503, content={"reason": "quota_exceeded", "message": str(exc)}
    )
