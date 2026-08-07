"""Onboarding library analysis and new-playlist selection (U9, F5, R7).

The user sees AI-proposed new-playlist candidates (name/theme/estimated
count, no songs attached) alongside their existing playlists, and can also
add a custom playlist by natural-language description on the same screen.
Only the selection step creates real (empty) playlist rows; nothing here
ever creates a review_queue item or touches YouTube.
"""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_default_user, get_library_analysis_service
from app.models.user import User
from app.services.library_analysis import (
    LibraryAnalysisService,
    PlaylistRemovalRequiresConfirmationError,
    run_reorganize_clustering,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class PlaylistProposalResponse(BaseModel):
    name: str
    theme: str
    song_count: int
    confidence: float


class AddedPlaylistResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    rule: Optional[dict]


class YouTubePlaylistResponse(BaseModel):
    playlist_id: str
    title: str


class AnalysisResponse(BaseModel):
    proposals: list[PlaylistProposalResponse]
    existing_playlists: list[YouTubePlaylistResponse]
    added_playlists: list[AddedPlaylistResponse]


class AcceptedProposal(BaseModel):
    name: str
    theme: str


class CustomPlaylist(BaseModel):
    name: str
    description: str


class AdoptedPlaylist(BaseModel):
    playlist_id: str
    name: str


class SelectionRequest(BaseModel):
    accepted_proposals: list[AcceptedProposal] = []
    custom_playlists: list[CustomPlaylist] = []
    adopted_playlists: list[AdoptedPlaylist] = []
    removed_playlist_ids: list[int] = []
    # KTD7: playlist ids the user has explicitly confirmed removing despite
    # having non-terminal (pending/approved_pending_apply) review work still
    # referencing them -- omitted ids that turn out to need confirmation
    # cause a 409 (see select()) instead of silently proceeding.
    confirmed_removed_playlist_ids: list[int] = []


class CreatedPlaylistResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]


class SelectionResponse(BaseModel):
    created_playlists: list[CreatedPlaylistResponse]


@router.get("/analysis", response_model=AnalysisResponse)
def get_analysis(
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> AnalysisResponse:
    proposals = service.propose_new_playlists(user.id)
    added = service.list_added_playlists(user.id)
    existing_on_youtube = service.list_existing_youtube_playlists(user.id)
    return AnalysisResponse(
        proposals=[
            PlaylistProposalResponse(
                name=p.name, theme=p.theme, song_count=p.song_count, confidence=p.confidence
            )
            for p in proposals
        ],
        existing_playlists=[
            YouTubePlaylistResponse(playlist_id=pl["playlistId"], title=pl["title"])
            for pl in existing_on_youtube
        ],
        added_playlists=[
            AddedPlaylistResponse(id=pl.id, name=pl.name, description=pl.description, rule=pl.rule)
            for pl in added
        ],
    )


@router.post("/select", response_model=SelectionResponse)
def select(
    body: SelectionRequest,
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> SelectionResponse:
    try:
        created = service.complete_onboarding(
            user_id=user.id,
            accepted_proposals=[p.model_dump() for p in body.accepted_proposals],
            custom_playlists=[c.model_dump() for c in body.custom_playlists],
            adopted_playlists=[a.model_dump() for a in body.adopted_playlists],
            removed_playlist_ids=body.removed_playlist_ids,
            confirmed_removed_playlist_ids=body.confirmed_removed_playlist_ids,
        )
    except PlaylistRemovalRequiresConfirmationError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "removal_requires_confirmation",
                "playlist_id": exc.playlist_id,
                "playlist_name": exc.playlist_name,
                "pending_count": exc.pending_count,
            },
        )
    return SelectionResponse(
        created_playlists=[
            CreatedPlaylistResponse(id=pl.id, name=pl.name, description=pl.description)
            for pl in created
        ]
    )


class ReorganizeTriggerResponse(BaseModel):
    session_id: int
    clustering_status: str


class ReorganizeProposalResponse(BaseModel):
    name: str
    theme: str
    song_count: int


class ReorganizeStatusResponse(BaseModel):
    session_id: int
    clustering_status: str
    proposals: list[ReorganizeProposalResponse]


@router.post("/reorganize", response_model=ReorganizeTriggerResponse)
def trigger_reorganize(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_default_user),
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> ReorganizeTriggerResponse:
    """R1/R2/R3: fetches the complete liked-songs library, reuses-or-creates
    the user's open reorganize session, and starts clustering as a
    background task (KTD9) -- the response returns immediately with the
    session id so the frontend can start polling `/reorganize/{id}`.
    """
    reorganize_session, liked_songs = service.trigger_reorganize(user.id)
    background_tasks.add_task(run_reorganize_clustering, reorganize_session.id, liked_songs)
    return ReorganizeTriggerResponse(
        session_id=reorganize_session.id, clustering_status=reorganize_session.clustering_status
    )


@router.get("/reorganize/{session_id}", response_model=ReorganizeStatusResponse)
def get_reorganize_status(
    session_id: int,
    service: LibraryAnalysisService = Depends(get_library_analysis_service),
) -> ReorganizeStatusResponse:
    status = service.get_reorganize_status(session_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"reorganize session {session_id} not found")
    return ReorganizeStatusResponse(
        session_id=status.session_id,
        clustering_status=status.clustering_status,
        proposals=[
            ReorganizeProposalResponse(name=p.name, theme=p.theme, song_count=p.song_count)
            for p in status.proposals
        ],
    )
