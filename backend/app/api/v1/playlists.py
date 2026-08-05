"""Natural-language playlist creation (U6).

Creates a playlist from a plain-language description and immediately proposes
initial matches from the user's library — every proposal lands in the
review queue as status="pending" (R8/R11: always reviewed, never
auto-applied). Nothing here writes to YouTube; that only ever happens once a
human approves a review_queue item through the existing review-queue API.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_default_user, get_playlist_creation_service
from app.models.user import User
from app.services.playlist_creation import PlaylistCreationService

router = APIRouter(prefix="/playlists", tags=["playlists"])


class CreatePlaylistRequest(BaseModel):
    name: str
    description: str


class PlaylistResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    rule: Optional[dict]


class CreatePlaylistResponse(BaseModel):
    playlist: PlaylistResponse
    review_queue_items_created: int


@router.post("", response_model=CreatePlaylistResponse, status_code=201)
def create_playlist(
    payload: CreatePlaylistRequest,
    user: User = Depends(get_default_user),
    service: PlaylistCreationService = Depends(get_playlist_creation_service),
) -> CreatePlaylistResponse:
    result = service.create_playlist_and_propose_matches(
        user_id=user.id, name=payload.name, description=payload.description
    )
    return CreatePlaylistResponse(
        playlist=PlaylistResponse(
            id=result.playlist.id,
            name=result.playlist.name,
            description=result.playlist.description,
            rule=result.playlist.rule,
        ),
        review_queue_items_created=result.review_queue_items_created,
    )
