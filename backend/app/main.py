from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import router as api_v1_router
from app.repositories.review_queue_repository import VersionConflictError
from app.services.review_queue import ItemNotFoundError, StaleItemError

app = FastAPI(title="AI Music Organizer")

app.include_router(api_v1_router)


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
